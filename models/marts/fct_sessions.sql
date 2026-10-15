-- One row per web/app session. Raw parquet is produced by scripts/generate_data.py.
select
    session_id,
    customer_id,
    cast(session_date as date) as session_date,
    -- Deterministic 0..999 bucket on the primary key: lets the RCA crawler screen on a
    -- governed, reproducible sample (filter sample_bucket < 100 = 10%).
    cast(hash(session_id) % 1000 as integer) as sample_bucket,
    cast(platform as varchar) as platform,
    cast(country as varchar) as country,
    cast(region as varchar) as region,
    cast(storefront as varchar) as storefront,
    cast(channel as varchar) as channel,
    cast(device as varchar) as device,
    cast(app_version as varchar) as app_version,
    cast(landing_page_id as varchar) as landing_page_id,
    cast(is_test_traffic as varchar) as is_test_traffic,
    cast(checkout_status as varchar) as checkout_status,
    cast(ab_bucket as varchar) as ab_bucket,
    cast(experiment_arm as varchar) as experiment_arm,
    cast(network_type as varchar) as network_type,
    cast(referrer_domain as varchar) as referrer_domain,
    cast(ad_creative as varchar) as ad_creative,
    cast(hour_band as varchar) as hour_band,
    cast(screen_size as varchar) as screen_size,
    cast(utm_content as varchar) as utm_content,
    cast(browser_family as varchar) as browser_family,
    cast(os_locale as varchar) as os_locale,
    converted,
    revenue
from read_parquet('{{ env_var("RCA_RAW_DIR", "data/raw") }}/sessions.parquet')
