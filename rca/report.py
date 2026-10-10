"""Render an RCAResult as a Markdown incident report (and a compact JSON summary)."""

from __future__ import annotations

import math

from rca.attribution import Cause
from rca.engine import RCAResult
from rca.semantic_client import pretty_dimension


def _style(r: RCAResult) -> str:
    if r.metric.kind == "simple":
        return "int"
    return "pct" if r.detection.is_proportion else "dec"


def _fmt(value: float, style: str) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return {"pct": f"{value:.2%}", "int": f"{value:,.0f}", "dec": f"{value:,.2f}"}[style]


def _p(p: float | None) -> str:
    if p is None or math.isnan(p):
        return "n/a"
    return f"{p:.1e}" if p < 0.001 else f"{p:.3f}"


def _seg(c: Cause) -> str:
    return " & ".join(f"`{pretty_dimension(d)}={v}`" for d, v in c.dims)


def _seg_dims(dims) -> str:
    return " & ".join(f"`{pretty_dimension(d)}={v}`" for d, v in dims)


def _narrative(c: Cause, r: RCAResult) -> str:
    style, label = _style(r), r.metric.label.lower()
    if r.metric.kind == "simple":
        change = f" ({c.value1 / c.value0 - 1:+.0%})" if c.value0 else ""
        return f"{label} per day {_fmt(c.value0, style)} → {_fmt(c.value1, style)} in this segment{change}."
    vol = f"{c.volume_change:+.0%}" if not math.isnan(c.volume_change) else "n/a"
    rate = f"{label} {_fmt(c.value0, style)} → {_fmt(c.value1, style)}"
    rate_p = f", p = {_p(c.p_value)}" if not math.isnan(c.p_value) else ""
    if c.driver == "rate":
        return f"**Rate problem**: {rate}{rate_p}; traffic {vol}."
    overall0 = r.detection.baseline_value
    return (
        f"**Mix problem**: the segment's own {label} barely moved ({rate}{rate_p}) but its traffic changed "
        f"{vol}. It normally sits {'above' if c.value0 > overall0 else 'below'} average "
        f"({_fmt(c.value0, style)} vs {_fmt(overall0, style)}), so "
        f"{'losing' if c.volume_change < 0 else 'gaining'} that traffic moves the blended number."
    )


def to_markdown(r: RCAResult) -> str:
    d, m = r.detection, r.metric
    style = _style(r)
    lines = [
        f"# Root cause report: {m.label}",
        "",
        f"**Current** {d.current[0]} → {d.current[1]} vs **baseline** {d.baseline[0]} → {d.baseline[1]}",
        "",
        "| | Baseline | Current | Change | z |",
        "|---|---|---|---|---|",
        f"| {m.label}{' (per day)' if m.kind == 'simple' else ''} | {_fmt(d.baseline_value, style)} | "
        f"{_fmt(d.current_value, style)} | {d.pct_change:+.1%} | {d.z_score:+.1f} |",
        "",
    ]
    lines += [f"> {n}" for n in r.notes] + ([""] if r.notes else [])

    lines += [f"## Root causes ({r.explained:.0%} of the change explained)", ""]
    for i, c in enumerate(r.causes, 1):
        lines.append(
            f"{i}. {_seg(c)}: **{c.pct_of_change:.0%}** of the change "
            f"(evidence p = {_p(c.evidence_p)}). {_narrative(c, r)}"
        )
        if c.refined_from:
            parents = ", ".join(_seg_dims(p) for p in c.refined_from)
            lines.append(f"   - Drilled down from {parents}")
    if r.causes:
        verdict = "consistent with noise" if not r.residual_p < 0.05 else "still significant, so more causes are likely"
        if r.residual_pct < 0:
            verdict += "; it moved against the drop, e.g. underlying growth offsetting part of it"
        lines.append(
            f"{len(r.causes) + 1}. Everything else: {r.residual_pct:.0%} of the change "
            f"(population p = {_p(r.residual_p)}, {verdict})."
        )
    else:
        lines.append("No segment met the evidence bar; the change is not concentrated in any slice.")
    lines.append("")

    lines += [
        "## Search trace",
        "",
        "Each round analyses the population left after excluding earlier causes.",
        "",
        "| Round | Population | Population change p | Best segment | Segment evidence p | Decision |",
        "|---|---|---|---|---|---|",
    ]
    for rd in r.rounds:
        excluded = r.causes[: rd["round"] - 1]
        pop = "all" if not excluded else "excluding " + ", ".join(_seg(c) for c in excluded)
        best = _seg_dims(rd["picked"]) if rd["picked"] else "none ≥ 10% of change"
        decision = "accept" if rd.get("accepted") else "stop"
        lines.append(
            f"| {rd['round']} | {pop} | {_p(rd['population_p'])} | {best} | {_p(rd['evidence_p'])} | {decision} |"
        )
    lines.append("")

    lines += [
        "## Dimension ranking, first pass (Adtributor surprise)",
        "",
        "| Dimension | Surprise | Top value | % of change |",
        "|---|---|---|---|",
    ]
    for s in r.dimension_ranking:
        lines.append(f"| {pretty_dimension(s.dimension)} | {s.surprise:.4f} | {s.top_value} | {s.top_pct:+.0%} |")
    lines.append("")

    lines += [
        "## How this was produced",
        "",
        f"- Dimensions discovered from the semantic layer for `{m.name}`: "
        + ", ".join(f"`{x}`" for x in r.dimensions),
        f"- {r.queries_run} governed MetricFlow queries over {len(r.rounds)} rounds "
        f"({len(r.one_way)} single-dimension + {len(r.two_way)} pairwise slices per round) "
        f"in {r.seconds:.1f}s. No hand-written SQL.",
        f"- `{m.name}` resolved from the manifest as "
        + (f"`{m.numerator} / {m.denominator}`" if m.kind == "ratio" else f"additive `{m.numerator}`"),
        "",
    ]
    if r.sample_sql:
        lines += [
            "<details><summary>SQL MetricFlow compiled for the top cause's slice</summary>",
            "",
            "```sql",
            r.sample_sql.strip(),
            "```",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def _num(x: float | None):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def to_summary(r: RCAResult) -> dict:
    d = r.detection
    return {
        "metric": r.metric.name,
        "baseline": [str(x) for x in d.baseline],
        "current": [str(x) for x in d.current],
        "baseline_value": d.baseline_value,
        "current_value": d.current_value,
        "pct_change": d.pct_change,
        "z_score": d.z_score,
        "explained": r.explained,
        "residual_pct": _num(r.residual_pct),
        "residual_p": _num(r.residual_p),
        "causes": [
            {
                "segment": {pretty_dimension(k): v for k, v in c.dims},
                "pct_of_change": c.pct_of_change,
                "driver": c.driver,
                "evidence_p": _num(c.evidence_p),
            }
            for c in r.causes
        ],
        "queries_run": r.queries_run,
    }
