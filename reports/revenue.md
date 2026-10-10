# Root cause report: Revenue

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01

| | Baseline | Current | Change | z |
|---|---|---|---|---|
| Revenue (per day) | 43,757 | 34,093 | -22.1% | -6.4 |

## Root causes (107% of the change explained)

1. `channel=email`: **81%** of the change (evidence p = 6.3e-11). revenue per day 14,259 → 6,427 in this segment (-55%).
2. `country=DE` & `platform=ios`: **26%** of the change (evidence p = 3.6e-19). revenue per day 2,887 → 388 in this segment (-87%).
   - Drilled down from `country=DE`, `platform=ios`
3. Everything else: -7% of the change (population p = 0.594, consistent with noise).

## Search trace

Each round analyses the population left after excluding earlier causes.

| Round | Population | Population change p | Best segment | Segment evidence p | Decision |
|---|---|---|---|---|---|
| 1 | all | 1.5e-06 | `channel=email` | 6.3e-11 | accept |
| 2 | excluding `channel=email` | 0.095 | `country=DE` & `platform=ios` | 3.6e-19 | accept |
| 3 | excluding `channel=email`, `country=DE` & `platform=ios` | 0.594 | `country=US` & `platform=ios` | 0.125 | stop |

## Dimension ranking, first pass (Adtributor surprise)

| Dimension | Surprise | Top value | % of change |
|---|---|---|---|
| channel | 0.0093 | email | +81% |
| country | 0.0060 | DE | +48% |
| platform | 0.0009 | ios | +65% |
| signup_cohort | 0.0002 | 2026H1 | +20% |
| device | 0.0001 | mobile | +76% |
| customer_tier | 0.0000 | plus | +39% |

## How this was produced

- Dimensions discovered from the semantic layer for `revenue`: `customer__customer_tier`, `customer__signup_cohort`, `session__channel`, `session__country`, `session__device`, `session__platform`
- 74 governed MetricFlow queries over 3 rounds (6 single-dimension + 15 pairwise slices per round) in 4.1s. No hand-written SQL.
- `revenue` resolved from the manifest as additive `revenue`

<details><summary>SQL MetricFlow compiled for the top cause's slice</summary>

```sql
-- Read Elements From Semantic Model 'sessions'
-- Metric Time Dimension 'session_date'
-- Constrain Time Range to [2026-09-04T00:00:00, 2026-10-08T00:00:00]
-- Select: ['__revenue', 'session__channel', 'metric_time__day']
-- Select: ['__revenue', 'session__channel', 'metric_time__day']
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  DATE_TRUNC('day', session_date) AS metric_time__day
  , channel AS session__channel
  , SUM(revenue) AS revenue
FROM "rca"."main"."fct_sessions" sessions_src_10000
WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
GROUP BY
  DATE_TRUNC('day', session_date)
  , channel
```
</details>
