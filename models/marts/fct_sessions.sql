-- One row per web/app session. Raw parquet is produced by scripts/generate_data.py.
select
    session_id,
    customer_id,
    cast(session_date as date) as session_date,
    platform,
    country,
    channel,
    device,
    converted,
    revenue
from read_parquet('data/raw/sessions.parquet')
