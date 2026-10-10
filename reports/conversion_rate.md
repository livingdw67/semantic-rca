# Root cause report: Conversion rate

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01

| | Baseline | Current | Change | z |
|---|---|---|---|---|
| Conversion rate | 4.59% | 3.87% | -15.7% | -10.9 |

## Root causes (93% of the change explained)

1. `country=DE` & `platform=ios`: **55%** of the change (evidence p = 7.7e-14). **Rate problem**: conversion rate 5.58% → 0.62%, p = 1.0e-53; traffic -10%.
   - Drilled down from `country=DE`, `platform=ios`
2. `channel=email`: **38%** of the change (evidence p = 5.0e-20). **Mix problem**: the segment's own conversion rate barely moved (conversion rate 8.14% → 8.05%, p = 0.795) but its traffic changed -49%. It normally sits above average (8.14% vs 4.59%), so losing that traffic moves the blended number.
3. Everything else: 7% of the change (population p = 0.826, consistent with noise).

## Search trace

Each round analyses the population left after excluding earlier causes.

| Round | Population | Population change p | Best segment | Segment evidence p | Decision |
|---|---|---|---|---|---|
| 1 | all | 3.7e-08 | `country=DE` & `platform=ios` | 7.7e-14 | accept |
| 2 | excluding `country=DE` & `platform=ios` | 1.9e-05 | `channel=email` | 5.0e-20 | accept |
| 3 | excluding `country=DE` & `platform=ios`, `channel=email` | 0.826 | `customer_tier=free` & `signup_cohort=2026H2` | 0.095 | stop |

## Dimension ranking, first pass (Adtributor surprise)

| Dimension | Surprise | Top value | % of change |
|---|---|---|---|
| channel | 0.0089 | email | +53% |
| country | 0.0026 | DE | +59% |
| platform | 0.0004 | ios | +69% |
| signup_cohort | 0.0001 | 2024H2 | +21% |
| device | 0.0001 | mobile | +77% |
| customer_tier | 0.0000 | free | +44% |

## How this was produced

- Dimensions discovered from the semantic layer for `conversion_rate`: `customer__customer_tier`, `customer__signup_cohort`, `session__channel`, `session__country`, `session__device`, `session__platform`
- 74 governed MetricFlow queries over 3 rounds (6 single-dimension + 15 pairwise slices per round) in 4.9s. No hand-written SQL.
- `conversion_rate` resolved from the manifest as `orders / sessions`

<details><summary>SQL MetricFlow compiled for the top cause's slice</summary>

```sql
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  metric_time__day
  , session__platform
  , session__country
  , SUM(__orders) AS orders
  , SUM(__sessions) AS sessions
FROM (
  -- Read Elements From Semantic Model 'sessions'
  -- Metric Time Dimension 'session_date'
  -- Constrain Time Range to [2026-09-04T00:00:00, 2026-10-08T00:00:00]
  -- Select: ['__orders', '__sessions', 'session__country', 'session__platform', 'metric_time__day']
  -- Select: ['__orders', '__sessions', 'session__country', 'session__platform', 'metric_time__day']
  SELECT
    DATE_TRUNC('day', session_date) AS metric_time__day
    , platform AS session__platform
    , country AS session__country
    , CASE WHEN session_id IS NOT NULL THEN 1 ELSE 0 END AS __sessions
    , converted AS __orders
  FROM "rca"."main"."fct_sessions" sessions_src_10000
  WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
) subq_6
GROUP BY
  metric_time__day
  , session__platform
  , session__country
```
</details>
