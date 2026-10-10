# Root cause report: Orders

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01

| | Baseline | Current | Change | z |
|---|---|---|---|---|
| Orders (per day) | 475 | 370 | -22.1% | -6.7 |

## Root causes (107% of the change explained)

1. `channel=email`: **80%** of the change (evidence p = 1.3e-10). orders per day 155 → 71 in this segment (-54%).
2. `country=DE` & `platform=ios`: **26%** of the change (evidence p = 5.0e-17). orders per day 32 → 4 in this segment (-87%).
   - Drilled down from `country=DE`, `platform=ios`
3. Everything else: -7% of the change (population p = 0.542, consistent with noise).

## Search trace

Each round analyses the population left after excluding earlier causes.

| Round | Population | Population change p | Best segment | Segment evidence p | Decision |
|---|---|---|---|---|---|
| 1 | all | 1.7e-07 | `channel=email` | 1.3e-10 | accept |
| 2 | excluding `channel=email` | 0.052 | `country=DE` & `platform=ios` | 5.0e-17 | accept |
| 3 | excluding `channel=email`, `country=DE` & `platform=ios` | 0.542 | `customer_tier=plus` & `device=mobile` | 0.124 | stop |

## Dimension ranking, first pass (Adtributor surprise)

| Dimension | Surprise | Top value | % of change |
|---|---|---|---|
| channel | 0.0090 | email | +80% |
| country | 0.0052 | DE | +46% |
| platform | 0.0008 | ios | +65% |
| signup_cohort | 0.0002 | 2024H2 | +21% |
| device | 0.0001 | mobile | +76% |
| customer_tier | 0.0000 | free | +45% |

## How this was produced

- Dimensions discovered from the semantic layer for `orders`: `customer__customer_tier`, `customer__signup_cohort`, `session__channel`, `session__country`, `session__device`, `session__platform`
- 74 governed MetricFlow queries over 3 rounds (6 single-dimension + 15 pairwise slices per round) in 3.7s. No hand-written SQL.
- `orders` resolved from the manifest as additive `orders`

<details><summary>SQL MetricFlow compiled for the top cause's slice</summary>

```sql
-- Read Elements From Semantic Model 'sessions'
-- Metric Time Dimension 'session_date'
-- Constrain Time Range to [2026-09-04T00:00:00, 2026-10-08T00:00:00]
-- Select: ['__orders', 'session__channel', 'metric_time__day']
-- Select: ['__orders', 'session__channel', 'metric_time__day']
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  DATE_TRUNC('day', session_date) AS metric_time__day
  , channel AS session__channel
  , SUM(converted) AS orders
FROM "rca"."main"."fct_sessions" sessions_src_10000
WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
GROUP BY
  DATE_TRUNC('day', session_date)
  , channel
```
</details>
