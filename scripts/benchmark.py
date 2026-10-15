"""Benchmark the staged RCA on randomly drawn incidents, scored against planted ground truth.

The main dataset is one hand-built scenario, so passing its tests says little about incidents
nobody tuned the engine for. This script draws scenarios with 0-3 random incidents (a segment over
platform / country / channel / device; a conversion or traffic change; down or up), builds each
into its own warehouse, runs the pipeline on every additive and ratio metric and scores it:

* recall       material planted causes (true impact >= 2% of the metric) found as the exact
               segment; a coarser or finer segment is a near miss, reported separately
* precision    accepted causes that are a planted segment, exactly
* false alarms accepted causes in scenarios with no incident (every fifth scenario)
* sign, mechanism (rate vs mix, for ratio metrics), and how often the 95% CI on a cause's
               contribution covers its true impact

    python scripts/benchmark.py                                  # 30 scenarios, about 45 minutes
    python scripts/benchmark.py --scenarios 3 --metrics conversion_rate

Each scenario lives in bench/s<seed>/. Finished scenarios are skipped on a re-run, so an
interrupted benchmark resumes. Raw data and the warehouse are deleted after scoring unless --keep;
the ground truth, the per-metric reports, the RCA summaries and the scores stay, so --rescore can
re-score them without rebuilding anything.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.generate_data import (  # noqa: E402
    CHANNELS, COUNTRIES, DEVICE_GIVEN_PLATFORM, DEVICES, PLATFORMS, REGION, Incident, Scenario, generate,
)

METRICS = ["conversion_rate", "orders", "revenue", "revenue_per_session"]
LEVELS = {"platform": list(PLATFORMS), "country": list(COUNTRIES), "channel": list(CHANNELS), "device": DEVICES}
NULL_EVERY = 5


# --------------------------------------------------------------------------- scenarios
def segment_share(seg: dict[str, str]) -> float:
    """Expected share of sessions in a segment over the incident dimensions, before any incident."""
    total = 0.0
    for plat, pp in PLATFORMS.items():
        dev = dict(zip(DEVICES, DEVICE_GIVEN_PLATFORM["web" if plat == "web" else "app"]))
        for device, pd_ in dev.items():
            for country, pc in COUNTRIES.items():
                for channel, pch in CHANNELS.items():
                    cell = {"platform": plat, "device": device, "country": country, "channel": channel}
                    if all(cell[k] == v for k, v in seg.items()):
                        total += pp * pd_ * pc * pch
    return total


def canonical(seg: dict[str, str]) -> dict[str, str]:
    """Drop conditions implied by the others: device=desktop & platform=web is device=desktop."""
    seg = dict(seg)
    if not set(seg) <= set(LEVELS):
        return seg
    for d in sorted(seg):
        rest = {k: v for k, v in seg.items() if k != d}
        if rest and math.isclose(segment_share(rest), segment_share(seg)):
            seg = rest
    return seg


def distinct_from_parents(seg: dict[str, str]) -> bool:
    """Every condition removes at least a third of the rows the others select. Otherwise the
    segment and its parent describe nearly the same rows (ios & mobile is 85% of ios), and naming
    the parent is not a mistake."""
    share = segment_share(seg)
    return all(share <= 2 / 3 * segment_share({k: v for k, v in seg.items() if k != d}) for d in seg)


def draw_incidents(rng: np.random.Generator, null: bool) -> list[Incident]:
    if null:
        return []
    out: list[Incident] = []
    for _ in range(rng.choice([1, 2, 3], p=[0.40, 0.35, 0.25])):
        k = rng.choice([1, 2, 3], p=[0.35, 0.40, 0.25])  # conditions; redraw values, not k, until valid
        while True:
            dims = rng.choice(list(LEVELS), k, replace=False)
            seg = canonical({str(d): str(rng.choice(LEVELS[d])) for d in sorted(dims)})
            items = set(seg.items())
            # Nested incidents have no single right answer, so they are not drawn.
            if segment_share(seg) >= 0.01 and distinct_from_parents(seg) and not any(
                    items <= set(o.segment.items()) or items >= set(o.segment.items()) for o in out):
                break
        kind = "volume" if rng.random() < 0.3 else "rate"
        up = rng.random() < 0.25
        if kind == "rate":
            mult = rng.uniform(1.3, 1.8) if up else rng.uniform(0.1, 0.6)
        else:  # traffic is generated at 2/3 keep-probability, so it can rise by at most 1.5x
            mult = rng.uniform(1.25, 1.5) if up else rng.uniform(0.3, 0.7)
        out.append(Incident(seg, kind, round(float(mult), 2)))
    return out


# --------------------------------------------------------------------------- matching
def descriptions(label: str) -> list[dict[str, str]]:
    """'country=US & platform=ios = platform=ios & region=NA (excluding ...)' -> both descriptions."""
    label = label.split(" (excluding ")[0]
    return [dict(p.split("=", 1) for p in part.split(" & ")) for part in label.split(" = ")]


def normalise(seg: dict[str, str]) -> dict[str, str]:
    """Rewrite conditions that select exactly the rows of an incident dimension's value."""
    single = {r: c for c, r in REGION.items() if list(REGION.values()).count(r) == 1}
    out = {}
    for d, v in seg.items():
        if d == "storefront":
            d, v = "country", v.removeprefix("store_").upper()
        elif d == "region" and v in single:
            d, v = "country", single[v]
        elif d == "app_version" and v == "web":
            d = "platform"
        out[d] = v
    return out


def implied(seg: dict[str, str]) -> set[tuple[str, str]]:
    """The segment's conditions plus the ones they imply (a country's region, a version's platform)."""
    items = set(seg.items())
    for d, v in seg.items():
        if d == "country":
            items.add(("region", REGION[v]))
        elif d == "app_version":
            items.add(("platform", v.split("_")[0]))
        elif d == "device" and v == "desktop":
            items.add(("platform", "web"))
    return items


def relation(label: str, planted: dict[str, str]) -> Optional[str]:
    """'exact', 'finer' (a slice of the planted segment), 'coarser' (contains it) or None."""
    best = None
    for desc in descriptions(label):
        found = canonical(normalise(desc))
        if found == planted:
            return "exact"
        if set(planted.items()) <= implied(found):
            best = "finer"
        elif best is None and set(found.items()) <= implied(planted):
            best = "coarser"
    return best


# --------------------------------------------------------------------------- scoring
def score(truth: dict, summary: dict, metric: str, material: float) -> dict:
    gap = (summary["actual"] - summary["expected"]) / summary["actual"]  # anomaly, in units of the level
    accepted = []
    for c in summary["causes"]:
        ci = [float(x) * gap for x in c["ci"]] if None not in c["ci"] else [float("nan")] * 2
        accepted.append({"segment": c["segment"], "impact": float(c["share_of_anomaly"]) * gap,
                         "ci": sorted(ci), "mechanism": c["mechanism"].split(" ")[0].rstrip(":")})
    planted = []
    for p in truth["causes"]:
        impact = p["relative_impact"][metric]
        row = {"segment": p["segment"], "type": p["type"], "mult": p["mult"], "impact": impact,
               "material": abs(impact) >= material, "status": "missed"}
        for c in accepted:
            rel = relation(c["segment"], p["segment"])
            if rel == "exact":
                row |= {"status": "exact", "found": c["segment"], "estimate": c["impact"],
                        "sign_ok": math.copysign(1, c["impact"]) == math.copysign(1, impact),
                        "ci_covers": bool(c["ci"][0] <= impact <= c["ci"][1])}
                if metric in ("conversion_rate", "revenue_per_session"):
                    row["mechanism_ok"] = c["mechanism"] == ("rate" if p["type"] == "rate" else "mix")
                break
            if rel and row["status"] == "missed":
                row |= {"status": "near", "found": c["segment"], "relation": rel}
        if row["status"] == "missed":
            rejected = next((r for r in summary["rejected"] if relation(r["segment"], p["segment"]) == "exact"),
                            next((r for r in summary["rejected"] if relation(r["segment"], p["segment"])), None))
            row["why"] = (f"rejected: {rejected['segment']} -- {rejected['reason']}" if rejected
                          else "not proposed by the crawl")
        planted.append(row)
    for c in accepted:
        rels = [relation(c["segment"], p["segment"]) for p in truth["causes"]]
        c["verdict"] = "planted" if "exact" in rels else ("near" if any(rels) else "false")
    return {"anomaly_pct": summary["anomaly_pct"], "p": summary["p"], "anomalous": summary["anomalous"],
            "planted": planted, "accepted": accepted, "preflight": summary.get("preflight", []),
            "statements": summary["statements"], "seconds": summary["seconds"]}


def aggregate(results: list[dict], metric: str) -> dict:
    runs = [r["metrics"][metric] for r in results]
    planted = [p for run in runs for p in run["planted"] if p["material"]]
    exact = [p for p in planted if p["status"] == "exact"]
    accepted = [c for run in runs for c in run["accepted"]]
    nulls = [r["metrics"][metric] for r in results if not r["incidents"]]
    mech = [p["mechanism_ok"] for p in exact if "mechanism_ok" in p]

    def rate(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "metric": metric,
        "material": len(planted),
        "exact": len(exact),
        "near": sum(p["status"] == "near" for p in planted),
        "missed": sum(p["status"] == "missed" for p in planted),
        "recall": rate([p["status"] == "exact" for p in planted]),
        "accepted": len(accepted),
        "precision": rate([c["verdict"] == "planted" for c in accepted]),
        "false_causes": sum(c["verdict"] == "false" for c in accepted),
        "null_scenarios": len(nulls),
        "null_false_alarms": sum(bool(run["accepted"]) for run in nulls),
        "sign": rate([p["sign_ok"] for p in exact]),
        "mechanism": rate(mech),
        "ci_coverage": rate([p["ci_covers"] for p in exact]),
        "median_abs_error_pp": (statistics.median(abs(p["estimate"] - p["impact"]) for p in exact) * 100
                                if exact else None),
        "statements_median": statistics.median(run["statements"] for run in runs),
        "statements_max": max(run["statements"] for run in runs),
        "seconds_median": statistics.median(run["seconds"] for run in runs),
    }


# --------------------------------------------------------------------------- report
def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _segment(seg: dict[str, str]) -> str:
    return " & ".join(f"{k}={v}" for k, v in sorted(seg.items()))


def to_markdown(results: list[dict], summary: list[dict], args) -> str:
    n_null = sum(not r["incidents"] for r in results)
    n_inc = sum(len(r["incidents"]) for r in results)
    lines = [
        "# Benchmark: random incidents",
        "",
        f"{len(results)} scenarios (seed {args.seed}), {n_null} of them with no incident; {n_inc} incidents in "
        "the others. Each incident hits a random segment of platform, country, channel and device "
        "(1-3 conditions) with a conversion or traffic change, down or up, in the week of Oct 2-8, on top "
        "of the usual decoys: a German holiday in the same week, payday, seasonality, aliases and 20 "
        "noise dimensions. Generated by `scripts/benchmark.py`.",
        "",
        f"A planted cause is **material** for a metric when its true impact is at least {args.material:.0%} "
        "of the metric's level in the incident week. Recall counts only the exact segment; a near miss "
        "is a coarser or finer segment containing or inside it. Precision counts accepted causes that "
        "are exactly a planted segment, material or not.",
        "",
        "| Metric | Material causes | Found exactly | Near miss | Missed | Precision | False causes | "
        "False alarms (null scenarios) | Right sign | Right mechanism | 95% CI covers truth | "
        "Median abs. error | Statements | Time |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        err = "n/a" if s["median_abs_error_pp"] is None else f"{s['median_abs_error_pp']:.1f} pp"
        lines.append(
            f"| `{s['metric']}` | {s['material']} | {s['exact']} ({_pct(s['recall'])}) | {s['near']} | "
            f"{s['missed']} | {_pct(s['precision'])} ({s['accepted']} accepted) | {s['false_causes']} | "
            f"{s['null_false_alarms']}/{s['null_scenarios']} | {_pct(s['sign'])} | {_pct(s['mechanism'])} | "
            f"{_pct(s['ci_coverage'])} | {err} | {s['statements_median']:.0f} (max {s['statements_max']}) | "
            f"{s['seconds_median']:.0f}s |")
    lines += ["", "Mechanism is checked for ratio metrics only (rate vs mix).", ""]

    misses = [(r, m, p) for r in results for m in args.metrics for p in r["metrics"][m]["planted"]
              if p["material"] and p["status"] != "exact"]
    if misses:
        lines += ["## Near misses and misses", "",
                  "| Scenario | Metric | Planted | True impact | Outcome |", "|---|---|---|---|---|"]
        for r, m, p in misses:
            what = (f"near ({p['relation']}): {p['found']}" if p["status"] == "near" else f"missed, {p['why']}")
            lines.append(f"| s{r['seed']:04d} | `{m}` | {_segment(p['segment'])} ({p['type']} x{p['mult']}) | "
                         f"{p['impact']:+.1%} | {what} |")
        lines.append("")
    false = [(r, m, c) for r in results for m in args.metrics for c in r["metrics"][m]["accepted"]
             if c["verdict"] == "false"]
    if false:
        lines += ["## False causes", "", "| Scenario | Metric | Accepted segment | Estimated impact | Planted |",
                  "|---|---|---|---|---|"]
        for r, m, c in false:
            plant = "; ".join(_segment(i["segment"]) for i in r["incidents"]) or "nothing"
            lines.append(f"| s{r['seed']:04d} | `{m}` | {c['segment']} | {c['impact']:+.1%} | {plant} |")
        lines.append("")
    lines += ["## Scenarios", "", "| Scenario | Incidents |", "|---|---|"]
    for r in results:
        inc = "; ".join(f"{_segment(i['segment'])} {i['type']} x{i['mult']}" for i in r["incidents"]) or "none"
        lines.append(f"| s{r['seed']:04d} | {inc} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- running
def _dbt() -> str:
    exe = Path(sys.executable).with_name("dbt.exe" if os.name == "nt" else "dbt")
    return str(exe) if exe.exists() else "dbt"


def _env(d: Path) -> dict:
    return {**os.environ, "RCA_RAW_DIR": (d / "raw").as_posix(), "RCA_DUCKDB": str(d / "rca.duckdb")}


def _scored(d: Path, seed: int, metrics: list[str], material: float, timing: dict) -> dict:
    truth = json.loads((d / "ground_truth.json").read_text())
    summaries = json.loads((d / "rca.json").read_text())
    result = {
        "seed": seed,
        "incidents": truth["causes"],
        "metrics": {m: score(truth, summaries[m], m, material) for m in metrics},
        "timing": timing,
    }
    (d / "result.json").write_text(json.dumps(result, indent=2))
    return result


def run_scenario(k: int, args) -> dict:
    seed = args.seed * 1000 + k
    d = args.work_dir / f"s{seed:04d}"
    done = d / "result.json"
    rerun = args.rerun and (d / "rca.duckdb").exists()
    if done.exists() and not rerun:
        cached = json.loads(done.read_text())
        if set(args.metrics) <= set(cached["metrics"]):
            if args.rescore:
                return _scored(d, seed, args.metrics, args.material, cached.get("timing", {}))
            return cached
    timing = {}
    if not rerun:
        rng = np.random.default_rng([seed, 1])
        incidents = draw_incidents(rng, null=k % NULL_EVERY == NULL_EVERY - 1)
        t = time.perf_counter()
        generate(Scenario(seed=seed, incidents=incidents), d / "raw", d)
        timing["generate"] = time.perf_counter() - t
        t = time.perf_counter()
        subprocess.run([_dbt(), "run", "--quiet", "--profiles-dir", str(ROOT), "--project-dir", str(ROOT),
                        "--target-path", str(d / "target"), "--log-path", str(d / "logs")],
                       env=_env(d), cwd=ROOT, check=True)
        timing["dbt"] = time.perf_counter() - t
    t = time.perf_counter()
    # A fresh process per warehouse: dbt caches its adapter (and so the database path) per process.
    subprocess.run([sys.executable, __file__, "--worker", str(d), "--metrics", *args.metrics],
                   env=_env(d), cwd=ROOT, check=True)
    timing["rca"] = time.perf_counter() - t
    result = _scored(d, seed, args.metrics, args.material, timing)
    if not args.keep:
        for path in (d / "raw", d / "target", d / "logs"):
            shutil.rmtree(path, ignore_errors=True)
        (d / "rca.duckdb").unlink(missing_ok=True)
    return result


def worker(d: Path, metrics: list[str]) -> None:
    from rca.pipeline import PipelineConfig, run_rca
    from rca.report import to_markdown as report, to_summary
    from rca.semantic_client import SemanticLayer

    sl = SemanticLayer(ROOT)
    config = dataclasses.replace(PipelineConfig(), calendar_path=d / "holidays.csv")
    out = {}
    for m in metrics:
        r = run_rca(sl, m, config=config)
        (d / f"{m}.md").write_text(report(r), encoding="utf-8")
        out[m] = {**to_summary(r), "anomalous": bool(r.detection.is_anomalous)}
    (d / "rca.json").write_text(json.dumps(out, indent=2, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--metrics", nargs="+", default=METRICS)
    ap.add_argument("--material", type=float, default=0.02, help="min |true impact| for a cause to count")
    ap.add_argument("--work-dir", type=Path, default=ROOT / "bench")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "benchmark",
                    help="writes <out>.md and <out>.json")
    ap.add_argument("--keep", action="store_true", help="keep each scenario's raw data and warehouse")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score finished scenarios from their saved RCA output (after changing the scoring)")
    ap.add_argument("--rerun", action="store_true",
                    help="re-run the RCA on warehouses kept with --keep (after changing the engine)")
    ap.add_argument("--jobs", type=int, default=1, help="scenarios to run in parallel")
    ap.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        worker(args.worker, args.metrics)
        return 0

    args.work_dir = args.work_dir.resolve()
    if not (ROOT / "target" / "semantic_manifest.json").exists():
        subprocess.run([_dbt(), "parse", "--profiles-dir", str(ROOT), "--project-dir", str(ROOT)], cwd=ROOT, check=True)
    t0 = time.perf_counter()
    results = []
    # Threads are enough: generation is numpy and DuckDB, and dbt and the RCA run in subprocesses.
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for future in as_completed([pool.submit(run_scenario, k, args) for k in range(args.scenarios)]):
            r = future.result()
            results.append(r)
            found = ", ".join(f"{m} {sum(p['status'] == 'exact' for p in r['metrics'][m]['planted'] if p['material'])}"
                              f"/{sum(p['material'] for p in r['metrics'][m]['planted'])}" for m in args.metrics)
            print(f"s{r['seed']:04d}  {len(r['incidents'])} incidents  {found}  "
                  f"({time.perf_counter() - t0:.0f}s elapsed)", flush=True)
    results.sort(key=lambda r: r["seed"])

    summary = [aggregate(results, m) for m in args.metrics]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps({"summary": summary, "scenarios": results}, indent=2))
    args.out.with_suffix(".md").write_text(to_markdown(results, summary, args), encoding="utf-8")
    print(args.out.with_suffix(".md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
