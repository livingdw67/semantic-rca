"""Generate a synthetic e-commerce dataset with planted root causes and calendar decoys.

History: 2025-09-01 .. 2026-10-08 (~400 days, ~8k sessions/day, ~30 categorical dimensions).
Incident window: 2026-10-02 .. 2026-10-08. Four things go wrong at once:

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

Structural traps for the crawler:

* ``storefront`` is a 1:1 alias of ``country`` (equivalent explanations must be grouped, not
  arbitrarily suppressed), ``region`` is a coarser hierarchy over country.
* ``app_version`` is nested inside ``platform``; ``device`` is highly correlated with platform.
* ``landing_page_id`` has 2,500 values (an ID, not a dimension) and ``is_test_traffic`` is
  near-constant: both should be screened out before any slicing.
* 10 session-level and 10 customer-level noise dimensions with mixed cardinality.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 11
START = date(2025, 9, 1)
END = date(2026, 10, 8)
INCIDENT_START = date(2026, 10, 2)
BASE_DAILY_SESSIONS = 8_000
N_CUSTOMERS = 40_000

PLATFORMS = {"ios": 0.45, "android": 0.35, "web": 0.20}
COUNTRIES = {"US": 0.35, "DE": 0.18, "GB": 0.13, "FR": 0.12, "BR": 0.12, "IN": 0.10}
CHANNELS = {"organic": 0.28, "paid_search": 0.25, "tiktok": 0.14, "email": 0.18, "referral": 0.15}
REGION = {"US": "NA", "DE": "EU", "GB": "EU", "FR": "EU", "BR": "LATAM", "IN": "APAC"}
APP_VERSIONS = {"ios": {"ios_7.1": 0.4, "ios_7.2": 0.6}, "android": {"android_5.4": 0.5, "android_5.5": 0.5}}

TIERS = {"free": 0.60, "plus": 0.30, "pro": 0.10}
COHORTS = ["2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"]

CONV_BASE = 0.035
CONV_PLATFORM = {"ios": 1.15, "android": 0.90, "web": 1.00}
CONV_COUNTRY = {"US": 1.10, "DE": 1.05, "GB": 1.00, "FR": 0.95, "BR": 0.80, "IN": 0.70}
CONV_CHANNEL = {"organic": 1.00, "paid_search": 1.10, "tiktok": 0.70, "email": 2.20, "referral": 1.20}
CONV_TIER = {"free": 0.80, "plus": 1.30, "pro": 1.80}
AOV_TIER = {"free": 60.0, "plus": 85.0, "pro": 140.0}

# Noise dimensions: name -> cardinality. Values are drawn from a fixed random distribution.
SESSION_NOISE = {
    "ab_bucket": 2, "experiment_arm": 4, "network_type": 3, "referrer_domain": 15, "ad_creative": 40,
    "hour_band": 4, "screen_size": 5, "utm_content": 25, "browser_family": 6, "os_locale": 8,
}
CUSTOMER_NOISE = {
    "acquisition_source": 6, "loyalty_band": 4, "age_band": 6, "email_opt_in": 2, "score_decile": 10,
    "preferred_category": 12, "household_size": 5, "crm_cluster": 20, "nps_band": 3, "payment_method": 5,
}

# (date, country or ALL, name, volume multiplier, conversion multiplier)
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
HOLIDAY_SHOULDER = {"unity_day": 0.70}  # bridge days either side of the holiday


def _codes(rng, dist: dict[str, float], size: int) -> np.ndarray:
    p = np.array(list(dist.values()), dtype=float)
    return rng.choice(len(p), size=size, p=p / p.sum()).astype(np.int16)


def _noise_dist(rng, k: int) -> np.ndarray:
    return rng.dirichlet(np.full(k, 2.0))


def build_customers(rng) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "customer_id": np.arange(1, N_CUSTOMERS + 1),
            "customer_tier": pd.Categorical.from_codes(_codes(rng, TIERS, N_CUSTOMERS), list(TIERS)),
            "signup_cohort": pd.Categorical.from_codes(
                rng.integers(0, len(COHORTS), N_CUSTOMERS).astype(np.int16), COHORTS
            ),
        }
    )
    for name, k in CUSTOMER_NOISE.items():
        cats = [f"{name}_{i:02d}" for i in range(k)]
        df[name] = pd.Categorical.from_codes(rng.choice(k, N_CUSTOMERS, p=_noise_dist(rng, k)).astype(np.int16), cats)
    return df


def _holiday_multipliers(day: date) -> list[tuple[str, float, float]]:
    out = []
    for hday, scope, name, vol, conv in HOLIDAYS:
        if hday == day:
            out.append((scope, vol, conv))
        elif name in HOLIDAY_SHOULDER and abs((day - hday).days) == 1:
            out.append((scope, HOLIDAY_SHOULDER[name], 1.0))
    return out


def build_sessions(rng, customers: pd.DataFrame) -> pd.DataFrame:
    plat_keys, ctry_keys, chan_keys = list(PLATFORMS), list(COUNTRIES), list(CHANNELS)
    conv_plat = np.array([CONV_PLATFORM[k] for k in plat_keys])
    conv_ctry = np.array([CONV_COUNTRY[k] for k in ctry_keys])
    conv_chan = np.array([CONV_CHANNEL[k] for k in chan_keys])
    tier_codes = customers["customer_tier"].cat.codes.to_numpy()
    tier_keys = list(customers["customer_tier"].cat.categories)
    conv_tier = np.array([CONV_TIER[k] for k in tier_keys])
    aov_tier = np.array([AOV_TIER[k] for k in tier_keys])
    noise_p = {name: _noise_dist(rng, k) for name, k in SESSION_NOISE.items()}
    lp_p = 1.0 / np.arange(1, 2501) ** 1.1
    lp_p /= lp_p.sum()

    IOS, AND, WEB = (plat_keys.index(k) for k in ("ios", "android", "web"))
    US, DE = ctry_keys.index("US"), ctry_keys.index("DE")
    EMAIL, PAID = chan_keys.index("email"), chan_keys.index("paid_search")
    EU = [ctry_keys.index(c) for c, r in REGION.items() if r == "EU"]

    frames = []
    for i in range((END - START).days + 1):
        day = START + timedelta(days=i)
        weekend = day.weekday() >= 5
        incident = day >= INCIDENT_START
        n = int(BASE_DAILY_SESSIONS * (0.85 if weekend else 1.0) * (1 + 0.0005 * i) * rng.normal(1.0, 0.03))
        n = int(n * 1.5)  # oversample, then thin by holiday / incident volume effects below

        platform = _codes(rng, PLATFORMS, n)
        country = _codes(rng, COUNTRIES, n)
        channel = _codes(rng, CHANNELS, n)

        keep_p = np.full(n, 1 / 1.5)
        conv_mult = np.ones(n)
        for scope, vol, conv in _holiday_multipliers(day):
            mask = (
                np.ones(n, bool) if scope == "ALL"
                else np.isin(country, EU) if scope == "EU"
                else country == ctry_keys.index(scope)
            )
            keep_p[mask] *= vol
            conv_mult[mask] *= conv
        if incident:
            keep_p[channel == EMAIL] *= 0.5  # planted #2
        keep = rng.random(n) < np.minimum(keep_p, 1.0)
        platform, country, channel, conv_mult = platform[keep], country[keep], channel[keep], conv_mult[keep]
        n = len(platform)

        device = np.where(
            platform == WEB,
            rng.choice(3, n, p=[0.7, 0.2, 0.1]),  # desktop, mobile, tablet
            rng.choice([1, 2], n, p=[0.85, 0.15]),
        ).astype(np.int16)
        app_version = np.full(n, 4, np.int16)  # web
        ios, andr = platform == IOS, platform == AND
        app_version[ios] = rng.choice([0, 1], ios.sum(), p=[0.4, 0.6])
        app_version[andr] = rng.choice([2, 3], andr.sum(), p=[0.5, 0.5])
        cust_idx = rng.integers(0, N_CUSTOMERS, n)
        tier = tier_codes[cust_idx]

        p = CONV_BASE * conv_plat[platform] * conv_ctry[country] * conv_chan[channel] * conv_tier[tier] * conv_mult
        if weekend:
            p *= 0.95
        if day.day <= 3:
            p *= 1.10  # payday
        if incident:
            p = np.where(ios & (country == DE), p * 0.10, p)  # planted #1
            p = np.where(andr & (country == US) & (channel == PAID), p * 0.05, p)  # planted #3
            p = np.where(ios & (country == US), p * 1.30, p)  # planted #4 (offsetting)
        converted = rng.random(n) < p
        revenue = np.where(converted, rng.lognormal(np.log(aov_tier[tier]), 0.45), 0.0).round(2)

        frame = {
            "session_date": np.full(n, np.datetime64(day)),
            "customer_id": customers["customer_id"].to_numpy()[cust_idx],
            "platform": platform, "country": country, "channel": channel,
            "device": device, "app_version": app_version,
            "landing_page_id": rng.choice(2500, n, p=lp_p).astype(np.int16),
            "is_test_traffic": (rng.random(n) < 0.0005).astype(np.int16),
            "converted": converted.astype(np.int8),
            "revenue": revenue,
        }
        for name, pk in noise_p.items():
            frame[name] = rng.choice(len(pk), n, p=pk).astype(np.int16)
        frames.append(pd.DataFrame(frame))

    s = pd.concat(frames, ignore_index=True)
    s.insert(0, "session_id", np.arange(1, len(s) + 1))
    cat = pd.Categorical.from_codes
    s["platform"] = cat(s["platform"], plat_keys)
    s["country"] = cat(s["country"], ctry_keys)
    s["channel"] = cat(s["channel"], chan_keys)
    s["device"] = cat(s["device"], ["desktop", "mobile", "tablet"])
    s["app_version"] = cat(s["app_version"], ["ios_7.1", "ios_7.2", "android_5.4", "android_5.5", "web"])
    s["region"] = s["country"].map(REGION).astype("category")
    s["storefront"] = s["country"].map(lambda c: f"store_{c.lower()}").astype("category")
    s["landing_page_id"] = cat(s["landing_page_id"], [f"lp_{i:04d}" for i in range(2500)])
    s["is_test_traffic"] = cat(s["is_test_traffic"], ["false", "true"])
    for name, k in SESSION_NOISE.items():
        s[name] = cat(s[name], [f"{name}_{i:02d}" for i in range(k)])
    return s


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "data" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)

    customers = build_customers(rng)
    sessions = build_sessions(rng, customers)

    con = duckdb.connect()
    for name, df in {"customers": customers, "sessions": sessions}.items():
        con.register(name, df)
        con.execute(f"COPY (SELECT * FROM {name}) TO '{(out / f'{name}.parquet').as_posix()}' (FORMAT parquet)")

    holidays = pd.DataFrame(
        [(d, scope, name) for d, scope, name, *_ in HOLIDAYS], columns=["date", "scope", "holiday"]
    )
    holidays.to_csv(root / "data" / "holidays.csv", index=False)

    ground_truth = {
        "incident_window": [INCIDENT_START.isoformat(), END.isoformat()],
        "causes": [
            {"segment": {"platform": "ios", "country": "DE"}, "type": "rate", "direction": "down"},
            {"segment": {"channel": "email"}, "type": "mix", "direction": "down"},
            {"segment": {"platform": "android", "country": "US", "channel": "paid_search"}, "type": "rate",
             "direction": "down"},
            {"segment": {"platform": "ios", "country": "US"}, "type": "rate", "direction": "up"},
        ],
        "equivalent_dimensions": [["country", "storefront"]],
        "decoys": ["unity_day (DE, Oct 2-4)", "payday (Oct 2-3)", "weekly seasonality", "growth trend"],
        "screen_out": ["landing_page_id", "is_test_traffic", "sample_bucket"],
    }
    (root / "data" / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))
    print(f"Wrote {len(sessions):,} sessions and {len(customers):,} customers to {out}")


if __name__ == "__main__":
    main()
