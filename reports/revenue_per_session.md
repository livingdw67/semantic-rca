# Root cause report: Revenue per session

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01

| | Baseline | Current | Change | z |
|---|---|---|---|---|
| Revenue per session | 4.23 | 3.57 | -15.6% | -9.0 |

## Root causes (93% of the change explained)

1. `country=DE` & `platform=ios`: **53%** of the change (evidence p = 2.5e-18). **Rate problem**: revenue per session 5.05 → 0.60; traffic -10%.
   - Drilled down from `country=DE`, `platform=ios`
2. `channel=email`: **40%** of the change (evidence p = 5.0e-20). **Mix problem**: the segment's own revenue per session barely moved (revenue per session 7.51 → 7.33) but its traffic changed -49%. It normally sits above average (7.51 vs 4.23), so losing that traffic moves the blended number.
3. Everything else: 7% of the change (population p = 0.852, consistent with noise).

## Search trace

Each round analyses the population left after excluding earlier causes.

| Round | Population | Population change p | Best segment | Segment evidence p | Decision |
|---|---|---|---|---|---|
| 1 | all | 2.6e-06 | `country=DE` & `platform=ios` | 2.5e-18 | accept |
| 2 | excluding `country=DE` & `platform=ios` | 9.4e-04 | `channel=email` | 5.0e-20 | accept |
| 3 | excluding `country=DE` & `platform=ios`, `channel=email` | 0.852 | `country=US` & `platform=ios` | 0.189 | stop |

## Dimension ranking, first pass (Adtributor surprise)

| Dimension | Surprise | Top value | % of change |
|---|---|---|---|
| channel | 0.0091 | email | +54% |
| country | 0.0030 | DE | +62% |
| platform | 0.0004 | ios | +70% |
| signup_cohort | 0.0000 | 2026H1 | +23% |
| device | 0.0000 | mobile | +76% |
| customer_tier | 0.0000 | plus | +39% |

## How this was produced

- Dimensions discovered from the semantic layer for `revenue_per_session`: `customer__customer_tier`, `customer__signup_cohort`, `session__channel`, `session__country`, `session__device`, `session__platform`
- 74 governed MetricFlow queries over 3 rounds (6 single-dimension + 15 pairwise slices per round) in 4.7s. No hand-written SQL.
- `revenue_per_session` resolved from the manifest as `revenue / sessions`

<details><summary>SQL MetricFlow compiled for the top cause's slice</summary>

```sql
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  metric_time__day
  , session__platform
  , session__country
  , SUM(__revenue) AS revenue
  , SUM(__sessions) AS sessions
FROM (
  -- Read Elements From Semantic Model 'sessions'
  -- Metric Time Dimension 'session_date'
  -- Constrain Time Range to [2026-09-04T00:00:00, 2026-10-08T00:00:00]
  -- Select: ['__revenue', '__sessions', 'session__country', 'session__platform', 'metric_time__day']
  -- Select: ['__revenue', '__sessions', 'session__country', 'session__platform', 'metric_time__day']
  SELECT
    DATE_TRUNC('day', session_date) AS metric_time__day
    , platform AS session__platform
    , country AS session__country
    , CASE WHEN session_id IS NOT NULL THEN 1 ELSE 0 END AS __sessions
    , revenue AS __revenue
  FROM "rca"."main"."fct_sessions" sessions_src_10000
  WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
) subq_6
GROUP BY
  metric_time__day
  , session__platform
  , session__country
```
</details>
