"""Generate a synthetic e-commerce dataset with planted root causes and calendar decoys.

History: 2025-09-01 .. 2026-10-08 (~400 days, ~8k sessions/day, ~30 categorical dimensions).
Incident window: 2026-10-02 .. 2026-10-08. The default scenario plants four incidents:

1. iOS x DE        conversion x0.10   (rate)   payment SDK release broke one PSP
2. email           traffic    x0.50   (mix)    lifecycle campaign paused
3. android x US x paid_search
                   conversion x0.05   (rate)   deep-link bug: a 3-way interaction
4. iOS x US        conversion x1.30   (rate, UP) offsetting win that masks #1 in the iOS marginal

Decoys the engine must NOT blame, because a good baseline expects them:

* German Unity Day (Oct 3) falls inside the incident week: DE traffic dips around it. It
  happened last year too, so a holiday-aware forecast knows it is coming.
* Payday: conversion is ~10% higher on days 1-3 of every month, and Oct 2-3 are in the window.
* Weekly seasonality, a slow growth trend, Black Friday, Christmas, Labor Day, ...

Structural traps:

* ``storefront`` is a 1:1 alias of ``country``; ``region`` is a coarser hierarchy over country.
* ``app_version`` is nested inside ``platform``; ``device`` is highly correlated with platform.
* ``landing_page_id`` has 2,500 values (an ID) and ``is_test_traffic`` is near-constant.
* ``checkout_status`` is derived from the outcome itself (purchased / abandoned / browsed): any
  tool that "finds" it as a cause is being circular.
* 10 session-level and 10 customer-level noise dimensions with mixed cardinality.

``generate(scenario, ...)`` is also used by scripts/benchmark.py with randomly drawn incidents. For
every incident it records the *true* impact on each metric, in expectation over the generated
sessions, so a benchmark can tell "missed" from "too small to matter".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

PLATFORMS = {"ios": 0.45, "android": 0.35, "web": 0.20}
COUNTRIES = {"US": 0.35, "DE": 0.18, "GB": 0.13, "FR": 0.12, "BR": 0.12, "IN": 0.10}
CHANNELS = {"organic": 0.28, "paid_search": 0.25, "tiktok": 0.14, "email": 0.18, "referral": 0.15}
DEVICES = ["desktop", "mobile", "tablet"]
DEVICE_GIVEN_PLATFORM = {"web": [0.70, 0.20, 0.10], "app": [0.0, 0.85, 0.15]}  # app = ios or android
REGION = {"US": "NA", "DE": "EU", "GB": "EU", "FR": "EU", "BR": "LATAM", "IN": "APAC"}
TIERS = {"free": 0.60, "plus": 0.30, "pro": 0.10}
COHORTS = ["2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"]

CONV_BASE = 0.035
CONV_PLATFORM = {"ios": 1.15, "android": 0.90, "web": 1.00}
CONV_COUNTRY = {"US": 1.10, "DE": 1.05, "GB": 1.00, "FR": 0.95, "BR": 0.80, "IN": 0.70}
CONV_CHANNEL = {"organic": 1.00, "paid_search": 1.10, "tiktok": 0.70, "email": 2.20, "referral": 1.20}
CONV_TIER = {"free": 0.80, "plus": 1.30, "pro": 1.80}
AOV_TIER = {"free": 60.0, "plus": 85.0, "pro": 140.0}

SESSION_NOISE = {
    "ab_bucket": 2, "experiment_arm": 4, "network_type": 3, "referrer_domain": 15, "ad_creative": 40,
    "hour_band": 4, "screen_size": 5, "utm_content": 25, "browser_family": 6, "os_locale": 8,
}
CUSTOMER_NOISE = {
    "acquisition_source": 6, "loyalty_band": 4, "age_band": 6, "email_opt_in": 2, "score_decile": 10,
    "preferred_category": 12, "household_size": 5, "crm_cluster": 20, "nps_band": 3, "payment_method": 5,
}

# (date, country / EU / ALL, name, volume multiplier, conversion multiplier)
HOLIDAYS = [
    (date(2025, 9, 1), "US", "labor_day", 0.80, 1.0),
    (date(2026, 9, 7), "US", "labor_day", 0.80, 1.0),
    (date(2025, 10, 3), "DE", "unity_day", 0.50, 1.0),
    (date(2026, 10, 3), "DE", "unity_day", 0.50, 1.0),
    (date(2025, 11, 27), "US", "thanksgiving", 0.70, 1.0),
    (date(2025, 11, 28), "ALL", "black_friday", 1.30, 1.40),
    (date(2025, 12, 25), "ALL", "christmas", 0.60, 1.0),
    (date(2026, 1, 1), "ALL", "new_year", 0.75, 1.0),
    (date(2026, 4, 6), "EU", "easter_monday", 0.80, 1.0),
]
HOLIDAY_SHOULDER = {"unity_day": 0.70}


@dataclass
class Incident:
    segment: dict[str, str]  # over platform / country / channel / device
    type: str  # "rate" (conversion multiplier) or "volume" (traffic multiplier)
    mult: float

    @property
    def direction(self) -> str:
        return "down" if self.mult < 1 else "up"


@dataclass
class Scenario:
    seed: int = 11
    start: date = date(2025, 9, 1)
    end: date = date(2026, 10, 8)
    incident_start: date = date(2026, 10, 2)
    daily_sessions: int = 8_000
    n_customers: int = 40_000
    incidents: list[Incident] = field(default_factory=lambda: [
        Incident({"platform": "ios", "country": "DE"}, "rate", 0.10),
        Incident({"channel": "email"}, "volume", 0.50),
        Incident({"platform": "android", "country": "US", "channel": "paid_search"}, "rate", 0.05),
        Incident({"platform": "ios", "country": "US"}, "rate", 1.30),
    ])


def _codes(rng, dist: dict[str, float], size: int) -> np.ndarray:
    p = np.array(list(dist.values()), dtype=float)
    return rng.choice(len(p), size=size, p=p / p.sum()).astype(np.int16)


def _noise_dist(rng, k: int) -> np.ndarray:
    return rng.dirichlet(np.full(k, 2.0))


def build_customers(rng, n: int) -> pd.DataFrame:
    df = pd.DataFrame({
        "customer_id": np.arange(1, n + 1),
        "customer_tier": pd.Categorical.from_codes(_codes(rng, TIERS, n), list(TIERS)),
        "signup_cohort": pd.Categorical.from_codes(rng.integers(0, len(COHORTS), n).astype(np.int16), COHORTS),
    })
    for name, k in CUSTOMER_NOISE.items():
        cats = [f"{name}_{i:02d}" for i in range(k)]
        df[name] = pd.Categorical.from_codes(rng.choice(k, n, p=_noise_dist(rng, k)).astype(np.int16), cats)
    return df


def _holiday_multipliers(day: date) -> list[tuple[str, float, float]]:
    out = []
    for hday, scope, name, vol, conv in HOLIDAYS:
        if hday == day:
            out.append((scope, vol, conv))
        elif name in HOLIDAY_SHOULDER and abs((day - hday).days) == 1:
            out.append((scope, HOLIDAY_SHOULDER[name], 1.0))
    return out


def _match(d: dict[str, np.ndarray], keys: dict[str, list[str]], segment: dict[str, str]) -> np.ndarray:
    m = np.ones(len(next(iter(d.values()))), bool)
    for dim, value in segment.items():
        m &= d[dim] == keys[dim].index(value)
    return m


def build_sessions(rng, customers: pd.DataFrame, sc: Scenario) -> tuple[pd.DataFrame, dict]:
    """Sessions for every day, plus each incident's expected impact on each metric.

    The random draws follow the original order, so the default scenario with seed 11 reproduces
    the published sample exactly. Anything newer (``checkout_status``) draws from its own stream,
    so adding a column can never reshuffle the sample again. The one exception: a traffic
    incident that targets ``device`` needs device drawn before the traffic is thinned, which
    changes the order for that scenario only (the benchmark draws such incidents).
    """
    keys = {"platform": list(PLATFORMS), "country": list(COUNTRIES), "channel": list(CHANNELS), "device": DEVICES}
    conv_plat = np.array([CONV_PLATFORM[k] for k in keys["platform"]])
    conv_ctry = np.array([CONV_COUNTRY[k] for k in keys["country"]])
    conv_chan = np.array([CONV_CHANNEL[k] for k in keys["channel"]])
    tier_codes = customers["customer_tier"].cat.codes.to_numpy()
    tier_keys = list(customers["customer_tier"].cat.categories)
    conv_tier = np.array([CONV_TIER[k] for k in tier_keys])
    aov_tier = np.array([AOV_TIER[k] for k in tier_keys])
    noise_p = {name: _noise_dist(rng, k) for name, k in SESSION_NOISE.items()}
    lp_p = 1.0 / np.arange(1, 2501) ** 1.1
    lp_p /= lp_p.sum()
    IOS, AND, WEB = (keys["platform"].index(k) for k in ("ios", "android", "web"))
    EU = [keys["country"].index(c) for c, r in REGION.items() if r == "EU"]
    extra = np.random.default_rng([sc.seed, 1])  # stream for columns added after the published sample
    device_first = any(inc.type == "volume" and "device" in inc.segment for inc in sc.incidents)

    def draw_device(platform: np.ndarray) -> np.ndarray:
        n = len(platform)
        return np.where(
            platform == WEB, rng.choice(3, n, p=DEVICE_GIVEN_PLATFORM["web"]),
            rng.choice([1, 2], n, p=DEVICE_GIVEN_PLATFORM["app"][1:]),
        ).astype(np.int16)

    # Each incident's impact over the incident window, in expectation given the sessions kept.
    impact = {i: {"dS": 0.0, "dO": 0.0, "dRev": 0.0} for i in range(len(sc.incidents))}
    totals = {"S": 0.0, "O": 0.0, "Rev": 0.0}

    frames = []
    for i in range((sc.end - sc.start).days + 1):
        day = sc.start + timedelta(days=i)
        weekend = day.weekday() >= 5
        incident = day >= sc.incident_start
        n = int(sc.daily_sessions * (0.85 if weekend else 1.0) * (1 + 0.0005 * i) * rng.normal(1.0, 0.03))
        n = int(n * 1.5)  # oversample, then thin by holiday / incident volume effects below

        d = {
            "platform": _codes(rng, PLATFORMS, n),
            "country": _codes(rng, COUNTRIES, n),
            "channel": _codes(rng, CHANNELS, n),
        }
        if device_first:
            d["device"] = draw_device(d["platform"])

        keep_p = np.full(n, 1 / 1.5)
        conv_mult = np.ones(n)
        for scope, vol, conv in _holiday_multipliers(day):
            mask = (np.ones(n, bool) if scope == "ALL" else np.isin(d["country"], EU) if scope == "EU"
                    else d["country"] == keys["country"].index(scope))
            keep_p[mask] *= vol
            conv_mult[mask] *= conv
        if incident:
            for inc in sc.incidents:
                if inc.type == "volume":
                    keep_p[_match(d, keys, inc.segment)] *= inc.mult
        keep = rng.random(n) < np.minimum(keep_p, 1.0)
        d = {k: v[keep] for k, v in d.items()}
        conv_mult = conv_mult[keep]
        n = len(d["platform"])

        if not device_first:
            d["device"] = draw_device(d["platform"])
        app_version = np.full(n, 4, np.int16)  # web
        ios, andr = d["platform"] == IOS, d["platform"] == AND
        app_version[ios] = rng.choice([0, 1], ios.sum(), p=[0.4, 0.6])
        app_version[andr] = rng.choice([2, 3], andr.sum(), p=[0.5, 0.5])
        cust_idx = rng.integers(0, sc.n_customers, n)
        tier = tier_codes[cust_idx]

        p = CONV_BASE * conv_plat[d["platform"]] * conv_ctry[d["country"]] * conv_chan[d["channel"]] * conv_tier[tier]
        p *= conv_mult
        if weekend:
            p *= 0.95
        if day.day <= 3:
            p *= 1.10  # payday
        if incident:
            masks = {k: _match(d, keys, inc.segment) for k, inc in enumerate(sc.incidents)}
            for k, inc in enumerate(sc.incidents):
                if inc.type == "rate":
                    p = np.where(masks[k], p * inc.mult, p)
            p = np.minimum(p, 1.0)
            aov = aov_tier[tier]
            totals["S"] += n
            totals["O"] += p.sum()
            totals["Rev"] += (p * aov).sum()
            for k, inc in enumerate(sc.incidents):
                m = masks[k]
                if inc.type == "volume":  # without it, 1 / mult times as many of these sessions
                    dq = 1 - 1 / inc.mult
                    impact[k]["dS"] += dq * m.sum()
                    impact[k]["dO"] += dq * p[m].sum()
                    impact[k]["dRev"] += dq * (p[m] * aov[m]).sum()
                else:  # without it, these sessions convert at p / mult
                    dp = p[m] - p[m] / inc.mult
                    impact[k]["dO"] += dp.sum()
                    impact[k]["dRev"] += (dp * aov[m]).sum()
        converted = rng.random(n) < p
        revenue = np.where(converted, rng.lognormal(np.log(aov_tier[tier]), 0.45), 0.0).round(2)

        frame = {
            "session_date": np.full(n, np.datetime64(day)),
            "customer_id": customers["customer_id"].to_numpy()[cust_idx],
            **d,
            "app_version": app_version,
            "landing_page_id": rng.choice(2500, n, p=lp_p).astype(np.int16),
            "is_test_traffic": (rng.random(n) < 0.0005).astype(np.int16),
            "converted": converted.astype(np.int8),
            "revenue": revenue,
        }
        for name, pk in noise_p.items():
            frame[name] = rng.choice(len(pk), n, p=pk).astype(np.int16)
        # Outcome-derived: purchased iff converted. A circular "cause" the pre-flight must catch.
        frame["checkout_status"] = np.where(converted, 0, np.where(extra.random(n) < 0.15, 1, 2)).astype(np.int16)
        frames.append(pd.DataFrame(frame))

    s = pd.concat(frames, ignore_index=True)
    s.insert(0, "session_id", np.arange(1, len(s) + 1))
    cat = pd.Categorical.from_codes
    for dim, k in keys.items():
        s[dim] = cat(s[dim], k)
    s["app_version"] = cat(s["app_version"], ["ios_7.1", "ios_7.2", "android_5.4", "android_5.5", "web"])
    s["region"] = s["country"].map(REGION).astype("category")
    s["storefront"] = s["country"].map(lambda c: f"store_{c.lower()}").astype("category")
    s["landing_page_id"] = cat(s["landing_page_id"], [f"lp_{i:04d}" for i in range(2500)])
    s["is_test_traffic"] = cat(s["is_test_traffic"], ["false", "true"])
    s["checkout_status"] = cat(s["checkout_status"], ["purchased", "abandoned_cart", "browsed"])
    for name, k in SESSION_NOISE.items():
        s[name] = cat(s[name], [f"{name}_{i:02d}" for i in range(k)])

    # Impact of each incident on each metric, relative to the metric's actual window level.
    S, O, Rev = totals["S"], totals["O"], totals["Rev"]
    rel = []
    for k in range(len(sc.incidents)):
        dS, dO, dRev = impact[k]["dS"], impact[k]["dO"], impact[k]["dRev"]
        cr = O / S - (O - dO) / (S - dS)
        rel.append({
            "conversion_rate": cr / (O / S),
            "orders": dO / O,
            "revenue": dRev / Rev,
            "revenue_per_session": (Rev / S - (Rev - dRev) / (S - dS)) / (Rev / S),
        })
    return s, {"relative_impact": rel}


def generate(sc: Scenario, raw_dir: Path, meta_dir: Path) -> dict:
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(sc.seed)
    customers = build_customers(rng, sc.n_customers)
    sessions, acct = build_sessions(rng, customers, sc)

    con = duckdb.connect()
    for name, df in {"customers": customers, "sessions": sessions}.items():
        con.register(name, df)
        con.execute(f"COPY (SELECT * FROM {name}) TO '{(raw_dir / f'{name}.parquet').as_posix()}' (FORMAT parquet)")

    pd.DataFrame([(d, scope, name) for d, scope, name, *_ in HOLIDAYS],
                 columns=["date", "scope", "holiday"]).to_csv(meta_dir / "holidays.csv", index=False)
    truth = {
        "incident_window": [sc.incident_start.isoformat(), sc.end.isoformat()],
        "causes": [
            {**asdict(inc), "direction": inc.direction, "relative_impact": acct["relative_impact"][k]}
            for k, inc in enumerate(sc.incidents)
        ],
        "equivalent_dimensions": [["country", "storefront"]],
        "decoys": ["unity_day (DE, Oct 2-4)", "payday (Oct 2-3)", "weekly seasonality", "growth trend"],
        "screen_out": ["landing_page_id", "is_test_traffic", "sample_bucket", "checkout_status"],
        "sessions": len(sessions),
    }
    # The main scenario's incident types use the analyst's vocabulary: a traffic change in a
    # high-converting segment shows up in a ratio metric as a mix effect.
    for c in truth["causes"]:
        c["mechanism"] = "mix" if c["type"] == "volume" else "rate"
    (meta_dir / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    return truth


def main() -> None:
    truth = generate(Scenario(), ROOT / "data" / "raw", ROOT / "data")
    print(f"Wrote {truth['sessions']:,} sessions to {ROOT / 'data' / 'raw'}")


if __name__ == "__main__":
    main()
