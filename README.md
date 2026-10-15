# semantic-rca: automated root cause analysis on a semantic layer

## Why I built this

Every analyst knows the moment. A KPI drops, someone asks "why?", and the next day disappears into
slicing dashboards: by country, then device, then channel, then combinations of the three. The
answer is usually two or three things happening at once, and some of the first suspects turn out
to be innocent: a holiday, a payday, or a segment that's simply big.

I wanted a tool that runs that investigation the way a careful analyst would, on numbers the
business already trusts. So it reads its metrics from the dbt Semantic Layer (MetricFlow) instead
of hand-written SQL. If "conversion rate" has a governed definition, that's the one it uses.

It also brings together the parts of the job I care most about: analytics engineering on a
semantic layer, statistics that hold up, SQL that scales to a warehouse, and honest evaluation
against a known answer.

## What it finds

Point it at a metric and a week. It answers three questions: did something really change, where,
and how much of the change does each cause explain?

```
Conversion rate, 2026-10-02 .. 10-08         naive -11.4%   expected +3.1% (payday)   anomaly -14.0%  (p = 1e-10)

  channel=email                    +59%  [+40% .. +76%]  mix: email still converts, its traffic halved
  country=DE & platform=ios        +45%  [+30% .. +56%]  rate: checkout broke
  country=US & platform=ios        -32%  [-50% .. -13%]  rate, OFFSETTING: a win that masked part of the drop
  paid_search & US & android       +18%  [+13% .. +23%]  rate: a 3-way interaction
  everything else                  +11%

  rejected: ios & region=EU (loose superset of DE & ios), ios_7.2 & US (slice of ios & US),
            5 noise segments (no unique contribution)                 8 warehouse statements, 17 s
```

In plain terms: conversion fell 11.4%, but payday should have lifted it 3.1%, so the real gap is
14.0%, and it isn't noise. Four things explain it, each with its share of the gap and a bootstrap
interval:

- **Email traffic halved.** Email still converts well; there's just less of it. That's a *mix*
  effect: the blend of traffic changed, not how each channel behaves.
- **Checkout broke for iOS users in Germany.** A *rate* effect: same traffic, worse conversion.
- **iOS users in the US did better.** That good news hid part of the drop, so its share is negative.
- **Paid search on Android in the US.** A 3-way interaction you can't see in any single dimension.

The last lines are suspects it considered and cleared. All four causes are incidents I planted in
synthetic data, and it got each one's mechanism and direction right. None of the decoys was
blamed: a German holiday the same week, payday, seasonality, duplicate dimensions, a column
derived from the outcome, and 20 dimensions of pure noise.

## How it works

```
history ─► pre-flight ──────► forecast ─────────► crawl ─────────────► attribution
           can I trust it?    what was expected?  where did it change?  who gets the credit?
```

**1. Check the data before trusting it.** Three data problems produce causes that look convincing
and are completely wrong, so `rca/preflight.py` checks for them first and lists anything it finds
at the top of the report:

- **A late partition.** If the last day hasn't fully landed, it looks like a crash. Trailing days
  with less than 60% of that weekday's usual volume are left out of the window. Holidays are kept,
  because a quiet holiday isn't missing data.
- **A renamed category.** If "iOS" becomes "ios", one value vanishes and another appears. That's a
  data change, not a cause, so the dimension is excluded with the reason.
- **A column derived from the outcome.** `checkout_status` (purchased, abandoned, browsed) is
  defined by conversion, so it "explains" any change in conversion perfectly and tells you nothing.
  It gets flagged and excluded. A column like this can also be declared in the semantic model,
  which covers metrics where the circularity doesn't show, like a count of active customers.

Any exclusion can be overridden in the dimension's config.

**2. Work out what "normal" would have been.** Comparing this week with the last few is misleading
when this week has a payday or a holiday. So `rca/forecast.py` fits trend, day of week, day of
month and holidays as a **Huber-robust ridge regression**: *robust* so one strange day can't drag
the fit around, *ridge* so the trend doesn't chase noise. The leftover errors are modelled as
**AR(1)**, because today's error tends to look like yesterday's, which gives an honest sense of how
much a week can wobble on its own. (It's the decomposition Prophet uses, without pulling in Stan
for what is really a penalized linear regression.) The difference between actual and expected is
the **anomaly**, the part that needs explaining. Forecasts are made per segment too, which is why
the German holiday's dip isn't blamed on Germany.

**3. Search the slices without flooding the warehouse.** One query per dimension, then per pair,
runs to about 500 queries a round at 30 dimensions. Instead, MetricFlow compiles the governed SQL,
and a planner wraps it in **`GROUPING SETS`**, which returns dozens of breakdowns from one scan.
MetricFlow still owns the joins, filters and metric logic, and golden tests check the wrapped
results match MetricFlow queried directly. The crawl then narrows down like an analyst would:

- Drop dimensions that can't be causes, like IDs and near-constants, and collapse aliases
  (`storefront` is just `country` under another name).
- Score each segment by **surplus**: its share of the change minus its share of the traffic. A
  segment that is 57% of traffic and carries 63% of the drop is unremarkable. Surplus is tested
  against the forecast's noise level, so random drift in big segments never makes the shortlist.
  For rates, the mix effect is also tested on its own: a channel that lost half its traffic but
  converts a little better has a big mix effect even when its net surplus looks ordinary.
- Drill down with a **beam search**: keep the most suspicious segments and split each by every
  other dimension. A narrower segment replaces its parent only if it concentrates the effect, so
  "iOS" doesn't become "iOS & mobile" just because most iOS traffic is mobile.
- Check every combination of the dimensions that already look guilty. Interactions usually
  involve them (*effect heredity*, from experimental design), and that's how the 3-way cause turns
  up even though it looks like noise in any single dimension.

That's 8 to 11 warehouse statements in total, and the count stays flat as dimensions are added.

**4. Count unique customers correctly.** Metrics like active customers don't add up: someone who
shopped on Monday and Tuesday is one customer, not two. The search uses exact `COUNT(DISTINCT)`
(DuckDB's approximate version was off by 15-20% here and flipped the sign of the change).
Attribution uses **HyperLogLog** sketches built in SQL: tiny fingerprints of a set that merge by
taking a max, so any combination of segments can be counted locally without going back to the
warehouse.

**5. Split the credit fairly.** Causes overlap, and if you peel them off one at a time, the first
one takes credit for everything it touches. So all candidates are judged together on a **flag
cube**, one row per day and combination of candidate segments, fetched in a single query. For each
combination the engine asks "what if these segments had behaved normally?", separately for rate
(keep the traffic, restore the conversion) and volume (keep the conversion, restore the traffic).
Duplicates merge, loose supersets and thin slices give way to the real cause, and bystanders like
"mobile" drop out when leaving them out changes nothing (with Bonferroni-adjusted intervals). The
final split uses **Owen values**: Shapley values, game theory's way to share credit that adds up
exactly to the total, applied across causes and then split between rate and volume. Intervals come
from a **residual bootstrap**, which resamples each day's deviation from the forecast rather than
raw days, so four Saturdays never get compared against an expectation built for one.

## Results against a known answer

The synthetic data covers 400 days, 3.4M sessions and 32 dimensions, with four incidents planted
in the week of Oct 2-8 plus decoys (`scripts/generate_data.py`, `data/ground_truth.json`).

| Metric | Type | Anomaly vs forecast | Planted causes found | Also accepted | Statements | Time |
|---|---|---|---|---|---|---|
| `conversion_rate` | ratio | -14.0% | 4 of 4 | none | 8 | 17s |
| `orders` | additive | -21.9% | 4 of 4 | none | 8 | 19s |
| `revenue` | additive | -23.4% | 3 of 4 | none | 9 | 17s |
| `revenue_per_session` | ratio | -15.5% | 4 of 4 | none | 10 | 22s |
| `buyer_rate` | count-distinct ratio | -18.1% | 4 of 4 | none | 9 | 52s |
| `active_customers` | count-distinct | -5.2% | 1 of 1 (email) | 5 other segments | 11 | 82s |
| `conversion_rate`, quiet week | ratio | -0.2% (p=0.92) | none, correctly | none | 3 | 3s |

Two results worth explaining:

- **Revenue misses the offsetting iOS-US effect, and that's the statistics working.** Revenue is
  heavy-tailed (a few big orders swing it), so that effect's interval, -33% to +8% of the anomaly,
  doesn't clear the multiple-testing bar. The report still lists it under "considered and
  rejected". In revenue per session, where dividing by sessions damps the noise, it's significant.
- **`active_customers` finds email, then spreads the rest thinly** over other channels. Its
  baseline is the previous four weeks rather than a holiday-aware forecast, and in this synthetic
  data customers aren't tied to one country or channel, so segments overlap heavily.

## Try it

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\generate_data.py          # ~3.4M sessions, planted incidents + decoys
.venv\Scripts\dbt build --profiles-dir .               # DuckDB warehouse + semantic manifest + dbt tests
.venv\Scripts\python -m rca.cli --metric conversion_rate --out reports\conversion_rate.md
```

```powershell
python -m rca.cli --metric buyer_rate                      # count-distinct ratio (HyperLogLog path)
python -m rca.cli --metric revenue --json                  # machine-readable summary for alerting bots
python -m rca.cli --metric conversion_rate --current-end 2026-09-24   # quiet week: no causes
python -m pytest -q                                        # 47 unit + 18 end-to-end tests (~5 min)
```

Example reports, with the statement trace and the wrapped SQL, are in [`reports/`](reports/).

## Limitations

- Only DuckDB is tested. The Snowflake and BigQuery SQL templates follow the same structure but
  haven't run against a live warehouse.
- Count-distinct baselines are the previous weeks, not forecasts, and the HyperLogLog estimator
  skips HLL++ bias correction (about 1% bias on sets of 40k-80k).
- Additive metrics take about 20 s locally; count-distinct metrics take 1-1.5 minutes.
- It can only judge what the crawl proposes. A cause whose dimensions never look suspicious, and
  whose cells sit below the noise floor, won't be found.
- The results above are for one generated sample. On other samples of the same scenario, the
  weaker causes (email's mix effect, the offsetting iOS-US effect) can fall below the
  multiple-testing bar. `scripts/benchmark.py` measures recall across random incidents.

## Project layout

```
models/            dbt models + semantic models (32 dimensions; additive, ratio and count-distinct metrics)
rca/
  semantic_client  the only module that talks to MetricFlow: metadata, compile, execute
  planner          GROUPING SETS wrapping, bin-packing, flag cubes, SQL-side HLL registers
  crawl            screening, aliases, surplus z-scores, beam search, heredity closure
  forecast         robust seasonal regression, calendar, noise floor
  detect           naive vs expected vs anomaly
  shapley          counterfactual games, nesting, leave-one-out, Owen values
  sketch           HLL decode / merge / estimate
  preflight        incomplete days, renamed categories, outcome-derived dimensions
  pipeline         detect -> crawl -> flag cube -> attribute
  report, cli      Markdown + JSON output
  exhaustive*      brute-force reference engine, used to check the crawl
scripts/
  generate_data    synthetic data with planted incidents + decoys
  benchmark        random incidents in their own warehouses, scored against ground truth
tests/             unit tests (no warehouse) + end-to-end tests scored against ground truth
```
