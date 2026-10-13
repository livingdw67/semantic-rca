"""Render a staged-pipeline RCAResult as a Markdown incident report and a JSON summary."""

from __future__ import annotations

import math

from rca.pipeline import RCAResult
from rca.semantic_client import pretty_dimension


def _style(r: RCAResult) -> str:
    if r.metric.kind == "simple":
        return "int"
    return "pct" if r.detection.is_proportion else "dec"


def _fmt(value: float, style: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return {"pct": f"{value:.2%}", "int": f"{value:,.0f}", "dec": f"{value:,.3f}"}[style]


def _p(p: float) -> str:
    return "n/a" if p is None or math.isnan(p) else (f"{p:.1e}" if p < 0.001 else f"{p:.3f}")


def _share(x: float, total: float) -> float:
    return x / total if total else float("nan")


def _ci(c, total: float) -> str:
    lo, hi = sorted((_share(c.ci[0], total), _share(c.ci[1], total)))
    return "n/a" if math.isnan(lo) else f"{lo:+.0%} .. {hi:+.0%}"


def _driver(c, total: float) -> str:
    if set(c.phi_by_effect) == {"rate", "volume"}:
        r, v = (_share(c.phi_by_effect[k], total) for k in ("rate", "volume"))
        kind = "rate" if abs(r) >= abs(v) else "mix (traffic share)"
        return f"{kind}: rate {r:+.0%}, volume {v:+.0%}"
    return {"level": "level", "membership": "membership"}.get(c.driver(), c.driver())


def to_markdown(r: RCAResult) -> str:
    d, a, m, w = r.detection, r.attribution, r.metric, r.windows
    style = _style(r)
    unit = " per day" if m.kind == "simple" and not w.weekly else (" per week" if m.kind == "simple" else "")
    lines = [
        f"# Root cause report: {m.label}",
        "",
        f"**Current** {w.current[0]} → {w.current[1]} vs **baseline** {w.baseline[0]} → {w.baseline[1]}"
        + (" (4 rolling weeks; count-distinct metric)" if w.weekly else ""),
        "",
        "| | Value | vs naive baseline |",
        "|---|---|---|",
        f"| Naive baseline (prior {(w.baseline[1] - w.baseline[0]).days + 1} days) | {_fmt(d.naive, style)}{unit} | |",
        f"| Expected ({d.method}) | {_fmt(d.expected, style)}{unit} | {d.expected_pct:+.1%} |",
        f"| Actual | {_fmt(d.actual, style)}{unit} | {d.naive_pct:+.1%} |",
        "",
        f"**Anomaly vs expected: {d.anomaly_pct:+.1%}** (z = {d.z:+.1f}, p = {_p(d.p)}; "
        f"noise floor ±{d.noise_floor:.1%}). "
        + (f"Calendar effects (seasonality, holidays, trend) account for {d.expected_pct:+.1%} on their own, "
           f"so the naive comparison {'understates' if abs(d.anomaly_pct) > abs(d.naive_pct) else 'overstates'} "
           f"the incident." if d.method == "forecast" and abs(d.expected_pct) >= 0.005 else ""),
        "",
    ]
    lines += [f"> {n}" for n in r.notes] + ([""] if r.notes else [])
    if r.preflight:
        lines += ["**Pre-flight data checks**", "", *[f"- {p}" for p in r.preflight], ""]

    acc = sorted(a.accepted, key=lambda c: -abs(c.phi))
    total = a.anomaly
    lines += [f"## Root causes ({_share(a.explained, total):.0%} of the anomaly explained)", ""]
    if acc:
        lines += [
            "| # | Segment | Share of anomaly | 95% CI | Mechanism | Share of volume |",
            "|---|---|---|---|---|---|",
        ]
        for i, c in enumerate(acc, 1):
            share = _share(c.phi, total)
            seg = c.label + (" **(offsetting)**" if share < 0 else "")
            lines.append(f"| {i} | {seg} | **{share:+.0%}** | {_ci(c, total)} | {_driver(c, total)} | "
                         f"{c.share_of_volume:.1%} |")
        lines += ["", f"Everything else: {_share(a.residual, total):+.0%} of the anomaly.", ""]
        if any(_share(c.phi, total) < 0 for c in acc):
            lines += ["Negative shares moved *against* the anomaly: they masked part of it.", ""]
    else:
        lines += ["No segment met the evidence bar. The change is not concentrated in any slice the crawl found.", ""]

    rejected = [c for c in a.candidates if c.status != "accepted"]
    if rejected:
        lines += ["## Considered and rejected", "", "| Segment | Why |", "|---|---|"]
        lines += [f"| {c.label} | {c.reason} |" for c in rejected]
        lines.append("")

    cr = r.crawl
    lines += [
        "## How this was produced",
        "",
        f"- **{r.queries_run} warehouse statements** in {r.seconds:.1f}s; every one is MetricFlow-compiled SQL "
        "(the planner only wraps it with window buckets and GROUPING SETS).",
        f"- {len(cr.dims)} dimensions crawled after screening"
        + (": " + "; ".join(f"`{pretty_dimension(k)}` ({v})" for k, v in cr.screened.items()) if cr.screened else "")
        + ".",
    ]
    aliases = {k: v for k, v in cr.aliases.items() if v}
    if aliases:
        lines.append("- Aliases collapsed: " + "; ".join(
            f"`{pretty_dimension(k)}` = " + ", ".join(f"`{pretty_dimension(x)}`" for x in v) for k, v in aliases.items()))
    lines.append(f"- Expected values for attribution: {r.expected_method}.")
    lines += ["", "| Statement | Grouping sets | Rows | Seconds |", "|---|---|---|---|"]
    lines += [f"| {s['purpose']} | {s['grouping_sets'] or ''} | {s['rows']:,} | {s['seconds']:.2f} |"
              for s in r.statements]
    lines.append("")
    first = next((s for s in r.statements if s["purpose"].startswith("stage 1")), None)
    if first:
        lines += [
            "<details><summary>Stage 1 statement: MetricFlow SQL wrapped in GROUPING SETS</summary>",
            "", "```sql", first["sql"].strip(), "```", "</details>", "",
        ]
    if a.log:
        lines += ["<details><summary>Attribution log</summary>", "", *[f"- {x}" for x in a.log], "", "</details>", ""]
    return "\n".join(lines)


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def to_summary(r: RCAResult) -> dict:
    d, a = r.detection, r.attribution
    return {
        "metric": r.metric.name,
        "current": [str(x) for x in r.windows.current],
        "baseline": [str(x) for x in r.windows.baseline],
        "actual": d.actual,
        "expected": d.expected,
        "naive": d.naive,
        "anomaly_pct": d.anomaly_pct,
        "naive_pct": d.naive_pct,
        "z": d.z,
        "p": d.p,
        "causes": [
            {
                "segment": c.label,
                "share_of_anomaly": _num(_share(c.phi, a.anomaly)),
                "ci": [_num(_share(x, a.anomaly)) for x in sorted(c.ci)],
                "mechanism": _driver(c, a.anomaly),
            }
            for c in sorted(a.accepted, key=lambda c: -abs(c.phi))
        ],
        "rejected": [{"segment": c.label, "reason": c.reason} for c in a.candidates if c.status != "accepted"],
        "preflight": r.preflight,
        "residual_share": _num(_share(a.residual, a.anomaly)),
        "statements": r.queries_run,
        "seconds": r.seconds,
    }
