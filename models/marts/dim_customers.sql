select
    customer_id,
    customer_tier,
    signup_cohort
from read_parquet('data/raw/customers.parquet')
