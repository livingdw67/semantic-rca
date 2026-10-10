# Root cause report: Active customers

**Current** 2026-10-02 → 2026-10-08 vs **baseline** 2026-09-04 → 2026-10-01 (4 rolling weeks; count-distinct metric)

| | Value | vs naive baseline |
|---|---|---|
| Naive baseline (prior 28 days) | 31,872 per week | |
| Expected (forecast) | 31,902 per week | +0.1% |
| Actual | 30,239 per week | -5.1% |

**Anomaly vs expected: -5.2%** (z = -7.8, p = 5.0e-15; noise floor ±1.3%). 

## Root causes (100% of the anomaly explained)

| # | Segment | Share of anomaly | 95% CI | Mechanism | Share of volume |
|---|---|---|---|---|---|
| 1 | channel=email (excluding channel=email & country=DE) | **+51%** | +48% .. +54% | level | 10.4% |
| 2 | channel=email & country=DE | **+14%** | +12% .. +16% | level | 1.6% |
| 3 | channel=paid_search | **+11%** | +7% .. +16% | level | 27.9% |
| 4 | channel=organic | **+10%** | +5% .. +14% | level | 29.1% |
| 5 | channel=referral | **+8%** | +4% .. +11% | level | 16.5% |
| 6 | channel=tiktok | **+6%** | +3% .. +9% | level | 16.1% |

Everything else: -0% of the anomaly.

## Considered and rejected

| Segment | Why |
|---|---|
| channel=email & region=EU | slice of channel=email: the rest of it moves the same way (107% of the per-unit effect) |
| channel=paid_search & country=US | slice of channel=paid_search: the rest of it moves the same way (143% of the per-unit effect) |
| channel=tiktok & region=NA | slice of channel=tiktok: the rest of it moves the same way (84% of the per-unit effect) |
| channel=tiktok & screen_size=screen_size_01 | slice of channel=tiktok: the rest of it moves the same way (284% of the per-unit effect) |
| ab_bucket=ab_bucket_01 & region=EU | no unique contribution given the others (LOO -0% of anomaly, CI -0%..-0%) |
| device=tablet | no unique contribution given the others (LOO -0% of anomaly, CI -0%..-0%) |

## How this was produced

- **11 warehouse statements** in 99.3s; every one is MetricFlow-compiled SQL (the planner only wraps it with window buckets and GROUPING SETS).
- 28 dimensions crawled after screening: `sample_bucket` (meta rca: exclude); `is_test_traffic` (near-constant (top value 99.62%)); `landing_page_id` (cardinality 2,500 > 500).
- Aliases collapsed: `country` = `storefront`
- Expected values for attribution: HLL sketches (p=14) vs each of 4 baseline weeks.

| Statement | Grouping sets | Rows | Seconds |
|---|---|---|---|
| detection: weekly distinct history |  | 58 | 0.29 |
| stage 0: dimension stats |  | 32 | 0.34 |
| stage 1: marginals | 29 | 1,130 | 4.27 |
| stage 2: level 2 | 48 | 16,045 | 12.16 |
| stage 2: level 2 | 48 | 4,330 | 20.54 |
| stage 2: level 2 | 6 | 280 | 2.05 |
| stage 2: level 3 | 48 | 66,566 | 34.58 |
| stage 2: level 3 | 27 | 9,825 | 17.50 |
| stage 2b: heredity closure | 4 | 990 | 1.79 |
| stage 3: flag sketches (HLL) |  | 180 | 0.64 |

<details><summary>Stage 1 statement: MetricFlow SQL wrapped in GROUPING SETS</summary>

```sql
with rca_mf as (
-- Join Standard Outputs
-- Select: [
--   '__active_customers',
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
--   'customer',
-- ]
-- Select: [
--   '__active_customers',
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
--   'customer',
-- ]
-- Aggregate Inputs for Simple Metrics
-- Compute Metrics via Expressions
-- Write to DataTable
SELECT
  subq_3.metric_time__day AS metric_time__day
  , subq_3.customer AS customer
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
  , COUNT(DISTINCT subq_3.__active_customers) AS active_customers
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
    , customer_id AS __active_customers
  FROM "rca"."main"."fct_sessions" sessions_src_10000
  WHERE DATE_TRUNC('day', session_date) BETWEEN '2026-09-04' AND '2026-10-08'
) subq_3
LEFT OUTER JOIN
  "rca"."main"."dim_customers" customers_src_10000
ON
  subq_3.customer = customers_src_10000.customer_id
GROUP BY
  subq_3.metric_time__day
  , subq_3.customer
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
rca_w as (select cast(floor(date_diff('day', cast(metric_time__day as date), DATE '2026-10-08') / 7) as integer) as rca_win, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, customer, active_customers from rca_mf)
select rca_win, grouping(customer__acquisition_source) as rca_g0, grouping(customer__age_band) as rca_g1, grouping(customer__crm_cluster) as rca_g2, grouping(customer__customer_tier) as rca_g3, grouping(customer__email_opt_in) as rca_g4, grouping(customer__household_size) as rca_g5, grouping(customer__loyalty_band) as rca_g6, grouping(customer__nps_band) as rca_g7, grouping(customer__payment_method) as rca_g8, grouping(customer__preferred_category) as rca_g9, grouping(customer__score_decile) as rca_g10, grouping(customer__signup_cohort) as rca_g11, grouping(session__ab_bucket) as rca_g12, grouping(session__ad_creative) as rca_g13, grouping(session__app_version) as rca_g14, grouping(session__browser_family) as rca_g15, grouping(session__channel) as rca_g16, grouping(session__country) as rca_g17, grouping(session__device) as rca_g18, grouping(session__experiment_arm) as rca_g19, grouping(session__hour_band) as rca_g20, grouping(session__network_type) as rca_g21, grouping(session__os_locale) as rca_g22, grouping(session__platform) as rca_g23, grouping(session__referrer_domain) as rca_g24, grouping(session__region) as rca_g25, grouping(session__screen_size) as rca_g26, grouping(session__storefront) as rca_g27, grouping(session__utm_content) as rca_g28, customer__acquisition_source, customer__age_band, customer__crm_cluster, customer__customer_tier, customer__email_opt_in, customer__household_size, customer__loyalty_band, customer__nps_band, customer__payment_method, customer__preferred_category, customer__score_decile, customer__signup_cohort, session__ab_bucket, session__ad_creative, session__app_version, session__browser_family, session__channel, session__country, session__device, session__experiment_arm, session__hour_band, session__network_type, session__os_locale, session__platform, session__referrer_domain, session__region, session__screen_size, session__storefront, session__utm_content, count(distinct case when active_customers > 0 then customer end) as n
from rca_w
where rca_win between 0 and 4
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

- nesting channel=email & region=EU in channel=email: rho=1.07 (CI 0.69..1.45) -> slice
- nesting channel=email & country=DE in channel=email: rho=0.63 (CI 0.32..0.95) -> both
- nesting channel=paid_search & country=US in channel=paid_search: rho=1.43 (CI -0.57..3.42) -> slice
- nesting channel=tiktok & region=NA in channel=tiktok: rho=0.84 (CI 0.35..1.33) -> slice
- nesting channel=tiktok & screen_size=screen_size_01 in channel=tiktok: rho=2.84 (CI -3.63..9.31) -> slice
- disjoint: channel=email (excluding channel=email & country=DE)
- eliminated ab_bucket=ab_bucket_01 & region=EU: no unique contribution given the others (LOO -0% of anomaly, CI -0%..-0%)
- eliminated device=tablet: no unique contribution given the others (LOO -0% of anomaly, CI -0%..-0%)

</details>
