# Root cause report: Conversion rate

**Current** 2026-09-18 → 2026-09-24 vs **baseline** 2026-08-21 → 2026-09-17

| | Value | vs naive baseline |
|---|---|---|
| Naive baseline (prior 28 days) | 4.60% | |
| Expected (forecast) | 4.58% | -0.5% |
| Actual | 4.57% | -0.7% |

**Anomaly vs expected: -0.2%** (z = -0.1, p = 0.921; noise floor ±4.1%). 

> p = 0.92: the change is within the noise floor (±4.1%). Attribution is shown anyway.

**Pre-flight data checks**

- `checkout_status` excluded: looks derived from the outcome: rates are all ~0% or ~100% ('purchased' 100%, 'abandoned_cart' 0%, 'browsed' 0%)

## Root causes (-0% of the anomaly explained)

No segment met the evidence bar. The change is not concentrated in any slice the crawl found.

## How this was produced

- **3 warehouse statements** in 2.6s; every one is MetricFlow-compiled SQL (the planner only wraps it with window buckets and GROUPING SETS).
- 28 dimensions crawled after screening: `sample_bucket` (meta rca: exclude); `is_test_traffic` (one value carries 99.95% of the metric: near-constant, or derived from the outcome); `landing_page_id` (cardinality 2,500 > 500); `checkout_status` (preflight: looks derived from the outcome: rates are all ~0% or ~100% ('purchased' 100%, 'abandoned_cart' 0%, 'browsed' 0%)).
- Aliases collapsed: `country` = `storefront`
- Expected values for attribution: none.

| Statement | Grouping sets | Rows | Seconds |
|---|---|---|---|
| stage 0: dimension stats |  | 33 | 0.14 |
| stage 1: marginals | 30 | 458 | 1.48 |

<details><summary>Stage 1 statement: MetricFlow SQL wrapped in GROUPING SETS</summary>

```sql
with rca_mf as (
-- Join Standard Outputs
-- Select: [
--   '__orders',
--   '__sessions',
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
--   'session__checkout_status',
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
--   '__sessions',
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
--   'session__checkout_status',
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
  subq_4.metric_time__day AS metric_time__day
  , subq_4.session__platform AS session__platform
  , subq_4.session__country AS session__country
  , subq_4.session__region AS session__region
  , subq_4.session__storefront AS session__storefront
  , subq_4.session__channel AS session__channel
  , subq_4.session__device AS session__device
  , subq_4.session__app_version AS session__app_version
  , subq_4.session__checkout_status AS session__checkout_status
  , subq_4.session__ab_bucket AS session__ab_bucket
  , subq_4.session__experiment_arm AS session__experiment_arm
  , subq_4.session__network_type AS session__network_type
  , subq_4.session__referrer_domain AS session__referrer_domain
  , subq_4.session__ad_creative AS session__ad_creative
  , subq_4.session__hour_band AS session__hour_band
  , subq_4.session__screen_size AS session__screen_size
  , subq_4.session__utm_content AS session__utm_content
  , subq_4.session__browser_family AS session__browser_family
  , subq_4.session__os_locale AS session__os_locale
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
  , SUM(subq_4.__orders) AS orders
  , SUM(subq_4.__sessions) AS sessions
FROM (
  -- Read Elements From Semantic Model 'sessions'
  -- Metric Time Dimension 'session_date'
  -- Constrain Time Range to [2026-08-21T00:00:00, 2026-09-24T00:00:00]
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
    , checkout_status AS session__checkout_status
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
    , CASE WHEN session_id IS NOT NULL THEN 1 ELSE 0 END AS __sessions
  FROM "rca"."main"."fct_sessions" sessions_src_10000
  WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-08-21' AND '2026-09-24'
) subq_4
LEFT OUTER JOIN
  "rca"."main"."dim_customers" customers_src_10000
ON
  subq_4.customer = customers_src_10000.customer_id
GROUP BY
  subq_4.metric_time__day
  , subq_4.session__platform
  , subq_4.session__country
  , subq_4.session__region
  , subq_4.session__storefront
  , subq_4.session__channel
  , subq_4.session__device
  , subq_4.session__app_version
  , subq_4.session__checkout_status
  , subq_4.session__ab_bucket
  , subq_4.session__experiment_arm
  , subq_4.session__network_type
  , subq_4.session__referrer_domain
  , subq_4.session__ad_creative
  , subq_4.session__hour_band
  , subq_4.session__screen_size
  , subq_4.session__utm_content
  , subq_4.session__browser_family
  , subq_4.session__os_locale
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
rca_w as (select case when metric_time__day >= DATE '2026-09-18' then 0 else 1 end as rca_win, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__checkout_status, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, orders, sessions from rca_mf)
select rca_win, grouping(customer__acquisition_source) as rca_g0, grouping(customer__age_band) as rca_g1, grouping(customer__crm_cluster) as rca_g2, grouping(customer__customer_tier) as rca_g3, grouping(customer__email_opt_in) as rca_g4, grouping(customer__household_size) as rca_g5, grouping(customer__loyalty_band) as rca_g6, grouping(customer__nps_band) as rca_g7, grouping(customer__payment_method) as rca_g8, grouping(customer__preferred_category) as rca_g9, grouping(customer__score_decile) as rca_g10, grouping(customer__signup_cohort) as rca_g11, grouping(session__ab_bucket) as rca_g12, grouping(session__ad_creative) as rca_g13, grouping(session__app_version) as rca_g14, grouping(session__browser_family) as rca_g15, grouping(session__channel) as rca_g16, grouping(session__checkout_status) as rca_g17, grouping(session__country) as rca_g18, grouping(session__device) as rca_g19, grouping(session__experiment_arm) as rca_g20, grouping(session__hour_band) as rca_g21, grouping(session__network_type) as rca_g22, grouping(session__os_locale) as rca_g23, grouping(session__platform) as rca_g24, grouping(session__referrer_domain) as rca_g25, grouping(session__region) as rca_g26, grouping(session__screen_size) as rca_g27, grouping(session__storefront) as rca_g28, grouping(session__utm_content) as rca_g29, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__checkout_status, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, sum(orders) as n, sum(sessions) as d
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
    (rca_win, session__checkout_status),
    (rca_win, session__device),
    (rca_win, session__network_type),
    (rca_win, session__platform),
    (rca_win, customer__email_opt_in),
    (rca_win, session__ab_bucket)
)
```
</details>

<details><summary>Attribution log</summary>

- no candidates

</details>
