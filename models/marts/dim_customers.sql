select
    customer_id,
    cast(customer_tier as varchar) as customer_tier,
    cast(signup_cohort as varchar) as signup_cohort,
    cast(acquisition_source as varchar) as acquisition_source,
    cast(loyalty_band as varchar) as loyalty_band,
    cast(age_band as varchar) as age_band,
    cast(email_opt_in as varchar) as email_opt_in,
    cast(score_decile as varchar) as score_decile,
    cast(preferred_category as varchar) as preferred_category,
    cast(household_size as varchar) as household_size,
    cast(crm_cluster as varchar) as crm_cluster,
    cast(nps_band as varchar) as nps_band,
    cast(payment_method as varchar) as payment_method
from read_parquet('data/raw/customers.parquet')
