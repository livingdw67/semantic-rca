select cast(range as date) as date_day
from range(date '2024-01-01', date '2027-12-31', interval 1 day)
