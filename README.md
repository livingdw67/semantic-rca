# semantic-rca: automated root cause analysis on a semantic layer

Dashboards tell you **what** happened ("conversion dropped 16% this week"). This engine tells you **why**,
in about 5 seconds, by crawling the dbt Semantic Layer (MetricFlow) like an analyst would:

```
1. country=DE & platform=ios: 55% of the change. Rate problem: conversion 5.58% -> 0.62% (p = 1e-53)
2. channel=email:             38% of the change. Mix problem: email still converts at 8.1%, but its
                                                traffic fell 49%, and email converts ~2x average
3. Everything else:            7% of the change (p = 0.83, consistent with noise)
```

Both answers are correct: they are the two incidents planted in the synthetic data (a broken iOS payment
flow in Germany, and a paused email campaign). The engine never sees the ground truth, only governed
metrics.

## Why a semantic layer is the right foundation

An RCA tool needs three things that raw SQL makes painful. The semantic layer provides all three:

| Need | Without a semantic layer | Here |
|---|---|---|
| **Which dimensions can slice this metric?** | Hard-coded column lists that go stale | `simple_dimensions_for_metrics()` discovers them, including `customer__customer_tier`, which is only reachable through the `customer` entity join |
| **How does the metric decompose?** | Re-implement `conversion = orders / sessions` in Python and hope it matches the dashboard | The manifest says `conversion_rate` is a ratio of `orders` / `sessions`. The engine fetches the parts and does exact math |
| **Correct SQL for 70+ slices and filters** | String-templated SQL, join bugs, fan-out | Every query is compiled by MetricFlow, the same definitions the BI layer uses |

Add a dimension or a metric in YAML and the engine picks it up with zero code changes.

## The method

```
detect ──► crawl semantic graph ──► decompose ──► drill down ──► test ──► peel off ──► repeat
```

1. **Detect.** Compare the last 7 days with the 28 days before (whole weeks, so weekly seasonality
   cancels). Report a z-score.
2. **Crawl.** For every categorical dimension, and every pair of dimensions, ask MetricFlow for the
   metric's components by day.
3. **Decompose exactly.** For a ratio metric R = N/D with segment traffic share *s* and rate *r*:

   ```
   R1 − R0 = Σᵢ  s1ᵢ(r1ᵢ − r0ᵢ)  +  (s1ᵢ − s0ᵢ)(r0ᵢ − R0)
                 └ rate effect ┘    └──── mix effect ────┘
   ```

   This is an identity, not an approximation: segment contributions always sum to the observed change.
   Because it is linear, it coincides with the Shapley attribution. Additive metrics (revenue, orders)
   decompose as plain per-day deltas.
4. **Drill down only when it is surprising.** Refine `platform=ios` into `ios & country=DE` only if the
   child keeps ≥ 75% of the parent's contribution *and* has lift ≥ 1.5 relative to its share of the
   parent's traffic. Without the lift test, "iOS" gets refined into "iOS & mobile", because most iOS
   traffic is mobile.
5. **Score by concentration.** `pct_of_change × (1 − segment share)` stops a huge segment like
   "mobile = 72% of traffic" from winning just by being huge.
6. **Require evidence.** A candidate is accepted only if its own movement is significant (weekday-adjusted
   Welch test at α = 0.01). Rate causes test the segment's rate. Mix causes test the segment's traffic.
7. **Peel off and repeat.** Exclude the accepted cause with a MetricFlow `where` filter
   (`NOT ({{ Dimension('session__country') }} = 'DE' AND …)`), then re-crawl what is left. This stops a
   dominant cause from smearing into every other dimension.
8. **Final exclusive attribution.** Re-decompose over disjoint segments (cause 1, cause 2 minus cause 1,
   …, rest), so the "% explained" numbers add up to exactly 100%.

Dimension ranking via Jensen–Shannon "surprise" follows *Adtributor* (Bhagwan et al., NSDI '14).

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\generate_data.py      # ~570k sessions with 2 planted incidents
.venv\Scripts\dbt build --profiles-dir .           # DuckDB warehouse + semantic manifest + tests
.venv\Scripts\python -m rca.cli --metric conversion_rate --out reports\conversion_rate.md
```

Other options:

```powershell
python -m rca.cli --metric revenue --json                  # machine-readable summary (for Slack/Jira bots)
python -m rca.cli --metric conversion_rate --current-end 2026-09-24   # a quiet week -> "no cause found"
python -m rca.cli --metric orders --depth 1                # single-dimension only
python -m pytest -q                                        # 20 tests: unit + end-to-end vs ground truth
```

Example reports for four metrics are in [`reports/`](reports/).

## Results against ground truth

| Metric | Type | Change | Causes found | Residual |
|---|---|---|---|---|
| conversion_rate | ratio | −15.7% | iOS×DE (rate) 55%, email (mix) 38% | 7%, p = 0.83 |
| revenue_per_session | ratio | −15.6% | iOS×DE (rate) 53%, email (mix) 40% | 7%, p = 0.85 |
| revenue | additive | −22.1% | email 81%, iOS×DE 26% | −7% (underlying growth trend) |
| orders | additive | −22.1% | email 80%, iOS×DE 26% | −7% (underlying growth trend) |
| conversion_rate, quiet week | ratio | noise | *none* (correctly) | n/a |

The ranking differs by metric for a real reason. Halving a high-value channel removes a lot of revenue
directly, but it moves the blended conversion rate only through mix.

## A note on statistical power

My first version of the dataset paused only 35% of a small email channel. That moved blended conversion
by about 2%, roughly 1.5σ of the noise over a 7-day window. The engine found only the iOS×DE incident and
correctly declined to claim the email one. That is the right behaviour: no method can attribute an effect
smaller than the noise floor, and an RCA tool that always produces a story is worse than none. If you need
to catch smaller shifts, use a longer current window.

## Project layout

```
models/
  marts/                 fct_sessions, dim_customers, metricflow_time_spine (+ dbt tests)
  semantic/              semantic models (sessions, customers) and metrics (simple + ratio)
rca/
  semantic_client.py     the only module that talks to MetricFlow: metadata, metric shapes, queries
  attribution.py         pure pandas math: decomposition, surprise, drill-down, tests
  engine.py              detect -> crawl -> peel loop -> exclusive attribution
  report.py / cli.py     Markdown + JSON output
scripts/generate_data.py synthetic data with planted incidents + data/ground_truth.json
tests/                   unit tests (no warehouse) + end-to-end tests scored against ground truth
```

## Pointing it at real data

Swap `profiles.yml` for your warehouse adapter (`dbt-metricflow[dbt-snowflake]`, etc.) and add your own
semantic models. The engine works with any metric that decomposes into additive parts: `simple` metrics
with `sum`/`count` and `ratio` metrics over them. It refuses count-distinct and other non-additive
aggregations rather than producing wrong attributions.

## Ideas for extension

- 3-way drill-down, only on dimensions that survived the 2-way pass (beam search)
- Post the JSON summary to Slack or open a Jira ticket when `detect()` fires (an hourly cron)
- Use `derived` metrics by expanding their expression tree into additive leaves
- Hierarchical dimensions (country → region) to report at the right level of the hierarchy
