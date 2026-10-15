"""The staged RCA pipeline: detect -> crawl -> flag cube -> joint attribution.

    1. history      1 statement   top-line series (daily, or weekly buckets for count-distinct)
    2. detect       local         robust seasonal forecast: expected value, anomaly, noise floor
    3. crawl        ~5 statements screen, marginals, beam levels, heredity closure (GROUPING SETS)
    4. flag cube    1-2 statements candidates' flags x day (additive) or HLL sketches (distinct)
    5. attribute    local         per-cell forecasts, nesting/LOO cleanup, Owen values, bootstrap

The statement count does not grow with the number of causes: everything after the flag cube is
local, including what the old engine did with one re-crawl per cause.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from rca.crawl import Crawler, CrawlConfig, CrawlResult, describe_segment
from rca.detect import Detection, detect
from rca.forecast import Calendar, forecast_total
from rca.planner import SlicePlanner, Windows
from rca.preflight import check_completeness, holiday_dates, volume_series
from rca.semantic_client import TIME_GROUP_BY, MetricShape, SemanticLayer
from rca.shapley import AdditiveGame, Attribution, SketchGame, attribute
from rca.sketch import decode

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class PipelineConfig:
    current_days: int = 7
    baseline_days: int = 28
    history_days: int = 400
    use_forecast: bool = True
    calendar_path: Path = ROOT / "data" / "holidays.csv"
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    n_boot: int = 200
    min_pct: float = 0.05
    sketch_precision: int = 14
    seed: int = 0
    allow_partial: bool = False  # keep trailing days that look incomplete
    prefer_general: bool = False  # experimental nesting rule, see shapley.attribute


@dataclass
class RCAResult:
    metric: MetricShape
    windows: Windows
    detection: Detection
    crawl: CrawlResult
    attribution: Attribution
    statements: list[dict]
    queries_run: int
    seconds: float
    expected_method: str
    notes: list[str] = field(default_factory=list)
    preflight: list[str] = field(default_factory=list)


def run_rca(
    sl: SemanticLayer,
    metric: str,
    current_end: Optional[dt.date] = None,
    config: PipelineConfig = PipelineConfig(),
) -> RCAResult:
    t0 = time.perf_counter()
    q0 = sl.queries_run
    shape = sl.metric_shape(metric)
    calendar = Calendar.from_csv(config.calendar_path) if config.use_forecast else Calendar()

    # 1. history and windows
    daily = sl.query(shape.base_metrics)
    end = current_end or max(daily[TIME_GROUP_BY])
    preflight: list[str] = []
    upto = daily[daily[TIME_GROUP_BY] <= end]
    completeness = check_completeness(upto[TIME_GROUP_BY], volume_series(upto, shape.base_metrics),
                                      holiday_dates(calendar))
    preflight += completeness.detail
    if completeness.excluded_days and not config.allow_partial:
        end = completeness.complete_end
    elif completeness.excluded_days:
        preflight.append("kept anyway (allow_partial)")
    hist_start = end - dt.timedelta(days=config.history_days - 1)
    windows = Windows.ending(end, config.current_days, config.baseline_days, weekly=shape.has_distinct)
    planner = SlicePlanner(sl, shape, windows)
    if shape.has_distinct:
        history = planner.history(hist_start)
    else:
        history = daily[daily[TIME_GROUP_BY].between(hist_start, end)].reset_index(drop=True)

    # 2. detect
    det = detect(history, shape, windows, calendar, use_forecast=config.use_forecast)

    # 3. crawl
    crawler = Crawler(sl, planner, config.crawl, contribution_se=det.contribution_se)
    crawl = crawler.run()
    segments = [c.dims for c in crawl.candidates]
    labels = [" = ".join(describe_segment(s) for s in [c.dims, *c.equivalents]) for c in crawl.candidates]

    # 4-5. flag cube and attribution
    notes = []
    if not det.is_anomalous:
        notes.append(f"p = {det.p:.2f}: the change is within the noise floor (±{det.noise_floor:.1%}). "
                     "Attribution is shown anyway.")
    if not segments:
        attribution = Attribution([], det.actual, det.expected, det.actual - det.expected, 0.0,
                                  det.actual - det.expected, (float("nan"),) * 2, ["no candidates"])
        method = "none"
    elif shape.has_distinct:
        attribution, method = _attribute_distinct(planner, shape, segments, labels, config)
    else:
        attribution, method = _attribute_additive(planner, shape, windows, segments, labels, calendar,
                                                  hist_start, config)
    preflight += crawler.preflight_notes
    preflight += [f"`{d.split('__')[-1]}` excluded: {r.removeprefix('preflight: ')}"
                  for d, r in crawl.screened.items() if r.startswith("preflight:")]
    return RCAResult(shape, windows, det, crawl, attribution, planner.statements, sl.queries_run - q0,
                     time.perf_counter() - t0, method, notes, preflight)


# --------------------------------------------------------------------------- additive
def _attribute_additive(planner, shape, windows, segments, labels, calendar, hist_start, config):
    cube = planner.flag_cube(segments, hist_start, windows.end)
    K = len(segments)
    fcols = [f"rca_f{i}" for i in range(K)]
    cube["cell"] = cube[fcols].astype(int).astype(str).agg("".join, axis=1)
    cells = sorted(cube["cell"].unique())
    flags = np.array([[c[i] == "1" for i in range(K)] for c in cells])
    days = pd.date_range(hist_start, windows.end, freq="D")
    has_d = shape.denominator is not None

    def pivot(col):
        return (cube.pivot_table(index="rca_day", columns="cell", values=col, aggfunc="sum")
                .reindex(index=days.date, columns=cells).fillna(0.0))

    N = pivot("n")
    D = pivot("d") if has_d else None
    cur = (N.index >= windows.current[0]) & (N.index <= windows.current[1])
    base = (N.index >= windows.baseline[0]) & (N.index <= windows.baseline[1])
    h = int(cur.sum())
    base_days, _ = windows.normalisers
    n1 = N[cur].sum().to_numpy()
    d1 = D[cur].sum().to_numpy() if has_d else None

    def expected(M: pd.DataFrame):
        """Daily expected values per cell over the current window: (h x cells), log sigmas, method."""
        naive_daily = np.tile(M[base].sum().to_numpy() / base_days, (h, 1))
        if not config.use_forecast:
            return naive_daily, np.zeros(len(cells)), "naive baseline window"
        top = forecast_total(days, M.sum(axis=1).to_numpy(), windows.current, calendar)
        out, sig = naive_daily.copy(), np.zeros(len(cells))
        modelled = np.zeros(len(cells), bool)
        for j, c in enumerate(cells):
            fc = forecast_total(days, M[c].to_numpy(), windows.current, calendar)
            if fc is not None:
                out[:, j], sig[j], modelled[j] = fc.daily, fc.log_sigma, True
        if top is not None:
            # Sparse cells: their baseline share of the top-line forecast, day by day.
            naive_total = naive_daily.sum(axis=1, keepdims=True)
            out[:, ~modelled] = naive_daily[:, ~modelled] * (top.daily[:, None] / np.maximum(naive_total, 1e-12))
            sig[~modelled] = top.log_sigma
            # Top-down reconciliation so the cells add up to the top-line forecast each day.
            out *= top.daily[:, None] / np.maximum(out.sum(axis=1, keepdims=True), 1e-12)
        # Cap forecast noise used for perturbation: a near-empty cell's model can be wild.
        return out, np.minimum(sig, 0.3), f"per-cell forecasts ({modelled.sum()}/{len(cells)} cells modelled, rest scaled)"

    nh_daily, sn, method = expected(N)
    dh_daily, sd = (expected(D)[:2] if has_d else (None, None))
    nh = nh_daily.sum(axis=0)
    dh = dh_daily.sum(axis=0) if has_d else None
    game = AdditiveGame(shape.kind, flags, n1, d1, nh, dh, per=h)

    # Residual bootstrap: resample the current window's daily deviations from the forecast, so
    # weekday/holiday structure stays in the expectation (resampling raw days would put four
    # Saturdays against an expectation computed for one).
    res_n = N[cur].to_numpy() - nh_daily
    res_d = D[cur].to_numpy() - dh_daily if has_d else None
    orig = (n1, d1, nh, dh)

    def resample(rng):
        idx = rng.integers(0, h, h)
        bn = nh + res_n[idx].sum(axis=0)
        bd = dh + res_d[idx].sum(axis=0) if has_d else None
        enh = nh * np.exp(rng.normal(0, sn))
        edh = dh * np.exp(rng.normal(0, sd)) if has_d else None
        game.set_data(np.maximum(bn, 0), np.maximum(bd, 0) if has_d else None, enh, edh)

    def restore():
        game.set_data(*orig)

    att = attribute(game, labels, resample, restore, n_boot=config.n_boot, min_pct=config.min_pct, seed=config.seed,
                    prefer_general=config.prefer_general)
    return att, method


# --------------------------------------------------------------------------- count-distinct
def _attribute_distinct(planner, shape, segments, labels, config):
    p = config.sketch_precision
    K = len(segments)
    W = planner.windows.n_baseline
    fcols = [f"rca_f{i}" for i in range(K)]
    sk = planner.flag_sketches(segments, precision=p)
    add = None
    if any(not shape.is_distinct(c) for c in shape.base_metrics):
        add = planner.flag_cube_weekly(segments)
    for df in (sk, add):
        if df is not None:
            df["cell"] = df[fcols].astype(int).astype(str).agg("".join, axis=1)
            df["rca_win"] = df["rca_win"].astype(int)
    cells = sorted(set(sk["cell"]) | (set(add["cell"]) if add is not None else set()))
    cidx = {c: j for j, c in enumerate(cells)}
    flags = np.array([[c[i] == "1" for i in range(K)] for c in cells])
    m = 1 << p

    def spec(component):
        if shape.is_distinct(component):
            cur = np.zeros((len(cells), m), np.uint8)
            base = np.zeros((W, len(cells), m), np.uint8)
            for _, r in sk[sk["rca_comp"] == component].iterrows():
                regs = decode(r["rca_regs"], p)
                if r["rca_win"] == 0:
                    cur[cidx[r["cell"]]] = regs
                else:
                    base[r["rca_win"] - 1, cidx[r["cell"]]] = regs
            return ("distinct", cur, base)
        alias = "n" if component == shape.numerator else "d"
        cur = np.zeros(len(cells))
        base = np.zeros((W, len(cells)))
        for _, r in add.iterrows():
            if r["rca_win"] == 0:
                cur[cidx[r["cell"]]] += float(r[alias])
            else:
                base[r["rca_win"] - 1, cidx[r["cell"]]] += float(r[alias])
        return ("additive", cur, base)

    comps = {"n": spec(shape.numerator)}
    if shape.denominator:
        comps["d"] = spec(shape.denominator)
    game = SketchGame(shape.kind, flags, comps, p)
    att = attribute(game, labels, min_pct=config.min_pct, seed=config.seed, prefer_general=config.prefer_general)
    return att, f"HLL sketches (p={p}) vs each of {W} baseline weeks"
