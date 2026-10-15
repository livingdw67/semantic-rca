# Root cause report: Orders

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01

| | Value | vs naive baseline |
|---|---|---|
| Naive baseline (prior 28 days) | 415 per day | |
| Expected (forecast) | 419 per day | +0.9% |
| Actual | 328 per day | -21.1% |

**Anomaly vs expected: -21.9%** (z = -9.5, p = 3.0e-21; noise floor ±5.2%). Calendar effects (seasonality, holidays, trend) account for +0.9% on their own, so the naive comparison understates the incident.

## Root causes (93% of the anomaly explained)

| # | Segment | Share of anomaly | 95% CI | Mechanism | Share of volume |
|---|---|---|---|---|---|
| 1 | channel=email | **+70%** | +54% .. +84% | level | 32.7% |
| 2 | country=DE & platform=ios | **+28%** | +20% .. +34% | level | 8.4% |
| 3 | country=US & platform=ios = platform=ios & region=NA **(offsetting)** | **-16%** | -27% .. -1% | level | 19.2% |
| 4 | channel=paid_search & country=US & platform=android = channel=paid_search & platform=android & region=NA | **+10%** | +8% .. +13% | level | 2.5% |

Everything else: +7% of the anomaly.

Negative shares moved *against* the anomaly: they masked part of it.

## Considered and rejected

| Segment | Why |
|---|---|
| channel=email & region=EU | slice of channel=email: the rest of it moves the same way (86% of the relative effect) |
| platform=ios & region=EU | loose superset of country=DE & platform=ios: the rest of it has no significant effect (1% of the relative effect) |
| channel=organic & country=US & platform=ios = channel=organic & platform=ios & region=NA | slice of country=US & platform=ios = platform=ios & region=NA: the rest of it moves the same way (85% of the relative effect) |
| app_version=ios_7.2 & country=US = app_version=ios_7.2 & region=NA | slice of country=US & platform=ios = platform=ios & region=NA: the rest of it moves the same way (214% of the relative effect) |
| app_version=android_5.5 & channel=paid_search & country=US = app_version=android_5.5 & channel=paid_search & region=NA | slice of channel=paid_search & country=US & platform=android = channel=paid_search & platform=android & region=NA: the rest of it moves the same way (91% of the relative effect) |

## How this was produced

- **8 warehouse statements** in 19.2s; every one is MetricFlow-compiled SQL (the planner only wraps it with window buckets and GROUPING SETS).
- 28 dimensions crawled after screening: `sample_bucket` (meta rca: exclude); `checkout_status` (one value carries 100.00% of the metric: near-constant, or derived from the outcome); `is_test_traffic` (one value carries 99.96% of the metric: near-constant, or derived from the outcome); `landing_page_id` (cardinality 2,500 > 500).
- Aliases collapsed: `country` = `storefront`
- Expected values for attribution: per-cell forecasts (14/15 cells modelled, rest scaled).

| Statement | Grouping sets | Rows | Seconds |
|---|---|---|---|
| stage 0: dimension stats |  | 33 | 0.11 |
| stage 1: marginals | 29 | 452 | 1.45 |
| stage 2: level 2 | 48 | 5,356 | 3.45 |
| stage 2: level 2 | 30 | 906 | 1.56 |
| stage 2: level 3 | 48 | 24,261 | 5.22 |
| stage 2: level 3 | 28 | 3,460 | 2.47 |
| stage 3: flag cube (daily) |  | 6,000 | 0.23 |

<details><summary>Stage 1 statement: MetricFlow SQL wrapped in GROUPING SETS</summary>

```sql
with rca_mf as (
-- Join Standard Outputs
-- Select: [
--   '__orders',
--   'customer__acquisition_source',
--   'customer__age_band',
--   'customer__crm_cluster',
--   'customer__customer_tier',
--   'customer__email_opt_in',
--   'customer__household_size',
--   'customer__loyalty_band',
--   'customer__nps_band',
--   'customer__payment_method',
--   'customer__preferred_category',
--   'customer__score_decile',
--   'customer__signup_cohort',
--   'session__ab_bucket',
--   'session__ad_creative',
--   'session__app_version',
--   'session__browser_family',
--   'session__channel',
--   'session__country',
--   'session__device',
--   'session__experiment_arm',
--   'session__hour_band',
--   'session__network_type',
--   'session__os_locale',
--   'session__platform',
--   'session__referrer_domain',
--   'session__region',
--   'session__screen_size',
--   'session__storefront',
--   'session__utm_content',
--   'metric_time__day',
-- ]
-- Select: [
--   '__orders',
--   'customer__acquisition_source',
--   'customer__age_band',
--   'customer__crm_cluster',
--   'customer__customer_tier',
--   'customer__email_opt_in',
--   'customer__household_size',
--   'customer__loyalty_band',
--   'customer__nps_band',
--   'customer__payment_method',
--   'customer__preferred_category',
--   'customer__score_decile',
--   'customer__signup_cohort',
--   'session__ab_bucket',
--   'session__ad_creative',
--   'session__app_version',
--   'session__browser_family',
--   'session__channel',
--   'session__country',
--   'session__device',
--   'session__experiment_arm',
--   'session__hour_band',
--   'session__network_type',
--   'session__os_locale',
--   'session__platform',
--   'session__referrer_domain',
--   'session__region',
--   'session__screen_size',
--   'session__storefront',
--   'session__utm_content',
--   'metric_time__day',
-- ]
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  subq_3.metric_time__day AS metric_time__day
  , subq_3.session__platform AS session__platform
  , subq_3.session__country AS session__country
  , subq_3.session__region AS session__region
  , subq_3.session__storefront AS session__storefront
  , subq_3.session__channel AS session__channel
  , subq_3.session__device AS session__device
  , subq_3.session__app_version AS session__app_version
  , subq_3.session__ab_bucket AS session__ab_bucket
  , subq_3.session__experiment_arm AS session__experiment_arm
  , subq_3.session__network_type AS session__network_type
  , subq_3.session__referrer_domain AS session__referrer_domain
  , subq_3.session__ad_creative AS session__ad_creative
  , subq_3.session__hour_band AS session__hour_band
  , subq_3.session__screen_size AS session__screen_size
  , subq_3.session__utm_content AS session__utm_content
  , subq_3.session__browser_family AS session__browser_family
  , subq_3.session__os_locale AS session__os_locale
  , customers_src_10000.customer_tier AS customer__customer_tier
  , customers_src_10000.signup_cohort AS customer__signup_cohort
  , customers_src_10000.acquisition_source AS customer__acquisition_source
  , customers_src_10000.loyalty_band AS customer__loyalty_band
  , customers_src_10000.age_band AS customer__age_band
  , customers_src_10000.email_opt_in AS customer__email_opt_in
  , customers_src_10000.score_decile AS customer__score_decile
  , customers_src_10000.preferred_category AS customer__preferred_category
  , customers_src_10000.household_size AS customer__household_size
  , customers_src_10000.crm_cluster AS customer__crm_cluster
  , customers_src_10000.nps_band AS customer__nps_band
  , customers_src_10000.payment_method AS customer__payment_method
  , SUM(subq_3.__orders) AS orders
FROM (
  -- Read Elements From Semantic Model 'sessions'
  -- Metric Time Dimension 'session_date'
  -- Constrain Time Range to [2026-09-04T00:00:00, 2026-10-08T00:00:00]
  SELECT
    DATE_TRUNC('day', session_date) AS metric_time__day
    , customer_id AS customer
    , platform AS session__platform
    , country AS session__country
    , region AS session__region
    , storefront AS session__storefront
    , channel AS session__channel
    , device AS session__device
    , app_version AS session__app_version
    , ab_bucket AS session__ab_bucket
    , experiment_arm AS session__experiment_arm
    , network_type AS session__network_type
    , referrer_domain AS session__referrer_domain
    , ad_creative AS session__ad_creative
    , hour_band AS session__hour_band
    , screen_size AS session__screen_size
    , utm_content AS session__utm_content
    , browser_family AS session__browser_family
    , os_locale AS session__os_locale
    , converted AS __orders
  FROM "rca"."main"."fct_sessions" sessions_src_10000
  WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
) subq_3
LEFT OUTER JOIN
  "rca"."main"."dim_customers" customers_src_10000
ON
  subq_3.customer = customers_src_10000.customer_id
GROUP BY
  subq_3.metric_time__day
  , subq_3.session__platform
  , subq_3.session__country
  , subq_3.session__region
  , subq_3.session__storefront
  , subq_3.session__channel
  , subq_3.session__device
  , subq_3.session__app_version
  , subq_3.session__ab_bucket
  , subq_3.session__experiment_arm
  , subq_3.session__network_type
  , subq_3.session__referrer_domain
  , subq_3.session__ad_creative
  , subq_3.session__hour_band
  , subq_3.session__screen_size
  , subq_3.session__utm_content
  , subq_3.session__browser_family
  , subq_3.session__os_locale
  , customers_src_10000.customer_tier
  , customers_src_10000.signup_cohort
  , customers_src_10000.acquisition_source
  , customers_src_10000.loyalty_band
  , customers_src_10000.age_band
  , customers_src_10000.email_opt_in
  , customers_src_10000.score_decile
  , customers_src_10000.preferred_category
  , customers_src_10000.household_size
  , customers_src_10000.crm_cluster
  , customers_src_10000.nps_band
  , customers_src_10000.payment_method
),
rca_w as (select case when metric_time__day >= DATE '2026-10-02' then 0 else 1 end as rca_win, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, orders from rca_mf)
select rca_win, grouping(customer__acquisition_source) as rca_g0, grouping(customer__age_band) as rca_g1, grouping(customer__crm_cluster) as rca_g2, grouping(customer__customer_tier) as rca_g3, grouping(customer__email_opt_in) as rca_g4, grouping(customer__household_size) as rca_g5, grouping(customer__loyalty_band) as rca_g6, grouping(customer__nps_band) as rca_g7, grouping(customer__payment_method) as rca_g8, grouping(customer__preferred_category) as rca_g9, grouping(customer__score_decile) as rca_g10, grouping(customer__signup_cohort) as rca_g11, grouping(session__ab_bucket) as rca_g12, grouping(session__ad_creative) as rca_g13, grouping(session__app_version) as rca_g14, grouping(session__browser_family) as rca_g15, grouping(session__channel) as rca_g16, grouping(session__country) as rca_g17, grouping(session__device) as rca_g18, grouping(session__experiment_arm) as rca_g19, grouping(session__hour_band) as rca_g20, grouping(session__network_type) as rca_g21, grouping(session__os_locale) as rca_g22, grouping(session__platform) as rca_g23, grouping(session__referrer_domain) as rca_g24, grouping(session__region) as rca_g25, grouping(session__screen_size) as rca_g26, grouping(session__storefront) as rca_g27, grouping(session__utm_content) as rca_g28, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, sum(orders) as n
from rca_w
where rca_win between 0 and 1
group by grouping sets (
    (rca_win, session__ad_creative),
    (rca_win, session__utm_content),
    (rca_win, customer__crm_cluster),
    (rca_win, session__referrer_domain),
    (rca_win, customer__preferred_category),
    (rca_win, customer__score_decile),
    (rca_win, session__os_locale),
    (rca_win, customer__acquisition_source),
    (rca_win, customer__age_band),
    (rca_win, customer__signup_cohort),
    (rca_win, session__browser_family),
    (rca_win, session__country),
    (rca_win, session__storefront),
    (rca_win, customer__household_size),
    (rca_win, customer__payment_method),
    (rca_win, session__app_version),
    (rca_win, session__channel),
    (rca_win, session__screen_size),
    (rca_win, customer__loyalty_band),
    (rca_win, session__experiment_arm),
    (rca_win, session__hour_band),
    (rca_win, session__region),
    (rca_win, customer__customer_tier),
    (rca_win, customer__nps_band),
    (rca_win, session__device),
    (rca_win, session__network_type),
    (rca_win, session__platform),
    (rca_win, customer__email_opt_in),
    (rca_win, session__ab_bucket)
)
```
</details>

<details><summary>Attribution log</summary>

- nesting country=DE & platform=ios in platform=ios & region=EU: rho=0.01 (CI -0.18..0.20) -> superset
- nesting channel=email & region=EU in channel=email: rho=0.86 (CI 0.43..1.29) -> slice
- nesting channel=organic & country=US & platform=ios = channel=organic & platform=ios & region=NA in country=US & platform=ios = platform=ios & region=NA: rho=0.85 (CI -0.03..1.73) -> slice
- nesting app_version=ios_7.2 & country=US = app_version=ios_7.2 & region=NA in country=US & platform=ios = platform=ios & region=NA: rho=2.14 (CI -29.14..33.41) -> slice
- nesting app_version=android_5.5 & channel=paid_search & country=US = app_version=android_5.5 & channel=paid_search & region=NA in channel=paid_search & country=US & platform=android = channel=paid_search & platform=android & region=NA: rho=0.91 (CI 0.51..1.31) -> slice

</details>
