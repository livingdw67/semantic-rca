"""Generate a synthetic e-commerce sessions dataset with planted root causes.

The data covers 2026-08-13 .. 2026-10-08. During the final 7 days two things go wrong:

1. Primary (rate effect): checkout conversion for iOS users in Germany collapses to 10% of
   normal -- think "payment SDK release broke Apple Pay for one PSP".
2. Secondary (mix effect): email traffic halves because a lifecycle campaign was paused.
   Email visitors convert well, so losing them drags the blended conversion rate down even
   though the email conversion rate itself does not change.

Everything else (weekly seasonality, noise, a slow upward trend) is a distractor. The
ground truth is written next to the data so the RCA engine can be scored against it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 7
START = date(2026, 8, 13)
END = date(2026, 10, 8)
INCIDENT_START = date(2026, 10, 2)
BASE_DAILY_SESSIONS = 10_000
N_CUSTOMERS = 20_000

PLATFORMS = {"ios": 0.45, "android": 0.35, "web": 0.20}
COUNTRIES = {"US": 0.33, "DE": 0.18, "GB": 0.14, "FR": 0.12, "BR": 0.13, "IN": 0.10}
CHANNELS = {"organic": 0.30, "paid_search": 0.22, "tiktok": 0.15, "email": 0.18, "referral": 0.15}
TIERS = {"free": 0.60, "plus": 0.30, "pro": 0.10}
COHORTS = ["2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"]

# Multiplicative conversion lifts -- the "normal" shape of the business.
CONV_BASE = 0.035
CONV_PLATFORM = {"ios": 1.15, "android": 0.90, "web": 1.00}
CONV_COUNTRY = {"US": 1.10, "DE": 1.05, "GB": 1.00, "FR": 0.95, "BR": 0.80, "IN": 0.70}
CONV_CHANNEL = {"organic": 1.00, "paid_search": 1.10, "tiktok": 0.70, "email": 2.20, "referral": 1.20}
CONV_TIER = {"free": 0.80, "plus": 1.30, "pro": 1.80}
AOV_TIER = {"free": 60.0, "plus": 85.0, "pro": 140.0}

INCIDENT_CONVERSION_MULT = 0.10  # iOS x DE
INCIDENT_EMAIL_VOLUME_MULT = 0.50  # email traffic


def _choice(rng: np.random.Generator, dist: dict[str, float], size: int) -> np.ndarray:
    keys = list(dist)
    probs = np.array(list(dist.values()))
    return rng.choice(keys, size=size, p=probs / probs.sum())


def build_customers(rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": np.arange(1, N_CUSTOMERS + 1),
            "customer_tier": _choice(rng, TIERS, N_CUSTOMERS),
            "signup_cohort": rng.choice(COHORTS, size=N_CUSTOMERS),
        }
    )


def build_sessions(rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    frames = []
    n_days = (END - START).days + 1
    for i in range(n_days):
        day = START + timedelta(days=i)
        weekly = 0.85 if day.weekday() >= 5 else 1.0
        trend = 1 + 0.002 * i
        n = int(BASE_DAILY_SESSIONS * weekly * trend * rng.normal(1.0, 0.04))
        incident = day >= INCIDENT_START

        channel = _choice(rng, CHANNELS, n)
        if incident:
            # Drop half of email sessions -> the campaign was paused.
            keep = ~((channel == "email") & (rng.random(n) > INCIDENT_EMAIL_VOLUME_MULT))
            channel = channel[keep]
            n = len(channel)

        platform = _choice(rng, PLATFORMS, n)
        country = _choice(rng, COUNTRIES, n)
        device = np.where(
            platform == "web",
            rng.choice(["desktop", "mobile", "tablet"], size=n, p=[0.7, 0.2, 0.1]),
            rng.choice(["mobile", "tablet"], size=n, p=[0.85, 0.15]),
        )
        cust_idx = rng.integers(0, N_CUSTOMERS, size=n)
        tier = customers["customer_tier"].to_numpy()[cust_idx]

        p = (
            CONV_BASE
            * pd.Series(platform).map(CONV_PLATFORM).to_numpy()
            * pd.Series(country).map(CONV_COUNTRY).to_numpy()
            * pd.Series(channel).map(CONV_CHANNEL).to_numpy()
            * pd.Series(tier).map(CONV_TIER).to_numpy()
        )
        if incident:
            p = np.where((platform == "ios") & (country == "DE"), p * INCIDENT_CONVERSION_MULT, p)

        converted = rng.random(n) < p
        aov = pd.Series(tier).map(AOV_TIER).to_numpy()
        revenue = np.where(converted, rng.lognormal(np.log(aov), 0.45), 0.0).round(2)

        frames.append(
            pd.DataFrame(
                {
                    "session_date": day,
                    "customer_id": customers["customer_id"].to_numpy()[cust_idx],
                    "platform": platform,
                    "country": country,
                    "channel": channel,
                    "device": device,
                    "converted": converted.astype(int),
                    "revenue": revenue,
                }
            )
        )

    sessions = pd.concat(frames, ignore_index=True)
    sessions.insert(0, "session_id", np.arange(1, len(sessions) + 1))
    return sessions


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
        con.execute(f"COPY {name} TO '{(out / f'{name}.parquet').as_posix()}' (FORMAT parquet)")

    ground_truth = {
        "incident_window": [INCIDENT_START.isoformat(), END.isoformat()],
        "causes": [
            {
                "segment": {"platform": "ios", "country": "DE"},
                "type": "rate",
                "description": f"Conversion multiplied by {INCIDENT_CONVERSION_MULT}",
            },
            {
                "segment": {"channel": "email"},
                "type": "mix",
                "description": f"Traffic multiplied by {INCIDENT_EMAIL_VOLUME_MULT}",
            },
        ],
    }
    (root / "data" / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))
    print(f"Wrote {len(sessions):,} sessions and {len(customers):,} customers to {out}")


if __name__ == "__main__":
    main()
