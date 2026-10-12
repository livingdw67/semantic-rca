# semantic-rca: automated root cause analysis on a semantic layer

Dashboards tell you **what** changed ("conversion is down 11% this week"). This engine tells you
**why**, by crawling the dbt Semantic Layer (MetricFlow) the way an analyst would. Every number it
uses comes from governed metric definitions, never hand-written SQL.

```
Conversion rate, 2026-10-02 .. 10-08         naive -11.4%   expected +3.1% (payday)   anomaly -14.0%  (p = 1e-10)

  channel=email                    +59%  [+40% .. +80%]  mix: email still converts, its traffic halved
  country=DE & platform=ios        +45%  [+30% .. +56%]  rate: checkout broke
  country=US & platform=ios        -32%  [-51% .. -12%]  rate, OFFSETTING: a win that masked part of the drop
  paid_search & US & android       +18%  [+13% .. +22%]  rate: a 3-way interaction
  everything else                  +11%

  rejected: ios & region=EU (loose superset of DE & ios), ios_7.2 & US (slice of ios & US),
            5 noise segments (no unique contribution)                 8 warehouse statements, 22 s
```

All four are the incidents planted in the synthetic data, with the right mechanism and sign.
None of the decoys is blamed: a German holiday in the same week, payday, seasonality, aliases,
loose supersets, and 20 noise dimensions.

**Why I built this.** When a KPI drops, the first question is always "why?", and answering it
usually costs an analyst a day of slicing dashboards. I built this to automate that work using the
same governed metric definitions a data team already trusts. It showcases the skills I bring to a
senior data science role:

- analytics engineering on a semantic layer (dbt, MetricFlow)
- statistics that hold up (robust forecasting, bootstrap intervals, Shapley and Owen attribution)
- warehouse-scale SQL (GROUPING SETS batching, HyperLogLog sketches)
- honest evaluation against planted ground truth

## What problem this solves, and how v2 scales it

v1 worked on a toy dataset but had four structural limits. v2 fixes each one:

| # | Problem in v1 | v2 |
|---|---|---|
| 1 | **Query explosion**: one query per dimension and per pair, repeated for every cause found (~500 per round at 30 dimensions) | A **SlicePlanner** compiles governed SQL with MetricFlow, wraps it in `GROUPING SETS`, and batches; a **staged crawl** (screen, marginals, beam search, heredity closure) needs **8-11 statements** in total |
| 2 | Count-distinct metrics rejected | **HyperLogLog sketches** built in SQL and merged client-side; exact `COUNT(DISTINCT)` for screening |
| 3 | Naive "last 7 vs previous 28 days" | A **robust seasonal forecast** (trend, weekday, monthly payday, holidays, AR(1) noise) sets the expected value, the anomaly size and the noise floor |
| 4 | Greedy peeling: the first correlated segment took all the credit | **Joint attribution**: counterfactual game on a flag cube, nesting and leave-one-out cleanup, **Owen values** with bootstrap CIs |

```
history ─► detect (forecast) ─► crawl ─────────────────────────────► flag cube ─► attribute
1 stmt     local                stage 0  screen        1 stmt        1-2 stmts    local
                                stage 1  marginals     1-2 stmts                  nesting / LOO
                                stage 2  beam levels   2-4 stmts                  Owen values
                                stage 2b closure       1 stmt                     bootstrap CIs
```

### 1. Query explosion: compile, wrap, batch

MetricFlow can't express `GROUPING SETS`, but it will compile any request to SQL
(`mf.explain(...)`). The planner asks it for the governed SQL at grain `day × {block of
dimensions}`, uses that verbatim as a CTE, and lets the warehouse roll it up into many grouping
sets in **one scan**. It then routes rows back to their logical request by `GROUPING()` bitmask.
MetricFlow still owns joins, filters and metric logic. The wrapper only sums components that the
manifest proved additive.

```sql
with rca_mf as ( <MetricFlow-compiled SQL> ),
rca_w as (select case when metric_time__day >= DATE '2026-10-02' then 0 else 1 end as rca_win, ... from rca_mf)
select rca_win, grouping(session__platform) as rca_g0, ..., sum(orders) as n, sum(sessions) as d
from rca_w group by grouping sets ((rca_win, session__platform), (rca_win, session__country), ...)
```

Requests are bin-packed under a cardinality budget and at most 48 sets per statement. Golden tests
assert that planner output equals MetricFlow queried directly.

The **crawl** keeps the statement count flat as dimensions grow:

- **Stage 0 screen** (1 statement). Drops IDs (`landing_page_id`, 2,500 values), near-constants
  (`is_test_traffic`) and anything tagged `meta: {rca: exclude}`.
- **Stage 1 marginals** (1 statement for all 29 screened dimensions). Collapses exact aliases:
  `storefront` is 1:1 with `country`, so it's reported, not crawled twice.
- **Scoring by surplus.** Surplus is a segment's share of the change minus its share of volume. A
  slice that is 57% of traffic and carries 63% of the drop is unremarkable. Surplus is z-tested
  against the forecast's noise floor, so random drift in big noise segments never enters the beam.
- **Stage 2 beam search.** Expands the top nodes along every other dimension. A child replaces its
  parent only if it concentrates the parent's *driver* (rate or mix effect). The lift test stops
  "iOS" becoming "iOS & mobile" just because most iOS traffic is mobile.
- **Stage 2b heredity closure.** Interactions are most likely among dimensions already implicated
  (the *effect heredity* principle from experimental design). One small statement fetches the dense
  sub-lattice over them. This is what finds the 3-way cause, whose 1- and 2-way marginals are
  diluted to z ≈ 1-2 by noise and by the offsetting iOS-US effect.
- **Successive halving** (`--screen-sample 0.2`), optional. Scores beam arms on a governed hash
  sample (`sample_bucket = hash(session_id) % 1000`) before paying for full fidelity. This is
  where a bandit framing actually fits: a full query returns its exact answer, so there is no
  reward noise to explore, but a sample has real sampling noise.

### 2. Count-distinct: mergeable sketches

`active_customers` and `buyers` are `count_distinct` measures. They aren't additive across days or
segments, so the engine handles them in three ways:

- **Windows** become rolling 7-day buckets (bucket 0 is current, 1-4 are the baseline weeks),
  because distinct counts don't scale with window length.
- **The crawl** uses exact `COUNT(DISTINCT)` inside `GROUPING SETS`. DuckDB's
  `approx_count_distinct` was off by 15-20% there and flipped the sign of the total change, so the
  approximate path is opt-in for warehouses with a well-behaved HLL.
- **Attribution** uses **HLL registers built in SQL** (`register = hash mod 2^14`,
  `rho = leading zeros + 1`, `max(rho)` per register), sparse-encoded into one string per cell.
  Registers merge with `MAX`, so any union of cells can be estimated locally.

The counterfactual never merges sketches from *different weeks*: that would count customers active
in either week and inflate the union. Instead, a reverted region changes the total by its own
change in distinct count, weighted by its *exclusivity*: the share of its customers found in no
other cell. Each metric must declare which entity it counts:

```yaml
config:
  meta:
    rca:
      distinct_entity: customer
```

### 3. Baselines: a robust seasonal forecast

`rca/forecast.py` fits Prophet's decomposition as a **Huber-robust ridge regression** in numpy:

- linear trend plus penalized changepoints every 8 weeks;
- day of week;
- monthly terms (day-of-month Fourier plus turn-of-month);
- one dummy per (holiday, offset) from a calendar table;
- AR(1) residuals for an honest window-level sigma.

Holt-Winters can't take holiday regressors, and Prophet pulls in Stan for what is a penalized
linear regression.

Detection reports three numbers: naive change, expected change and **anomaly**. For the incident
week, the naive comparison says −11.4%, but payday falls on Oct 2-3, so the forecast expected
+3.1% and the real anomaly is −14.0%. Attribution explains the anomaly, using **per-cell forecasts**
reconciled top-down to the top-line forecast. That is why the German holiday's traffic dip in the
same week is not blamed on Germany.

One fix worth calling out: a robust loss treats a holiday seen once last year as an outlier and
refuses to learn it. Rows the calendar explains keep full weight.

### 4. Attribution without sequential bias

Candidates are evaluated **jointly** on a *flag cube*: one row per (day × combination of candidate
flags), at most 2^K cells, fetched in one statement. A coalition of players "reverts" the cells
its segments touch to their expected values. Each candidate has a **rate** player (restore
conversion, keep traffic) and a **volume** player (restore traffic, keep conversion).

1. **Equivalent supports merge**, so `country=US` and `region=NA` are reported as one cause, not
   one arbitrarily suppressed.
2. **Nesting by effect homogeneity**, conditioned on the other causes. If B contains A, the bootstrap
   interval of ρ (B-minus-A's per-unit effect relative to A's) decides:
   - ρ ≈ 0: B is a **loose superset**, so keep A (`ios & EU` gives way to `ios & DE`).
   - ρ ≈ 1: A is a **slice**, so keep B (`ios_7.2 & US` gives way to `ios & US`).
   - Otherwise both are kept and made disjoint.
3. **Backward elimination** by leave-one-out contribution, with Bonferroni-adjusted bootstrap
   intervals. A big overlapping segment like "mobile" has no *unique* contribution once the real
   cause is present.
4. **Owen values** (Shapley across candidates, then split fairly between rate and volume). This is
   the unique attribution that is efficient and symmetric, so overlapping causes *share* their
   overlap rather than the first one taking it all.

Uncertainty comes from a **residual bootstrap**: it resamples daily deviations from the forecast,
not raw days. Resampling raw days put four Saturdays against an expectation computed for one,
which inflated every interval.

## Results against ground truth

The synthetic dataset covers 400 days, 3.4M sessions and 31 dimensions. It plants four incidents
in the week of Oct 2-8 and a set of decoys (`scripts/generate_data.py`, `data/ground_truth.json`).

| Metric | Type | Naive change | Anomaly vs forecast | Planted causes recovered | Other accepted | Statements | Time |
|---|---|---|---|---|---|---|---|
| `conversion_rate` | ratio | -11.4% | -14.0% (p=1e-10) | 4/4: email +59%, iOS×DE +45%, iOS×US (offset) -32%, 3-way +18% | none | 8 | 22s |
| `orders` | additive | -21.1% | -21.9% (p=3e-21) | 4/4: email +70%, iOS×DE +28%, iOS×US (offset) -16%, 3-way +10% | none | 8 | 20s |
| `revenue` | additive | -22.5% | -23.4% (p=2e-20) | 3/4: email +71%, iOS×DE +27%, 3-way +10% | none | 9 | 18s |
| `revenue_per_session` | ratio | -13.0% | -15.5% (p=8e-10) | 4/4: email +67%, iOS×DE +56%, iOS×US (offset) -36%, 3-way +12% | none | 10 | 28s |
| `buyer_rate` | count-distinct ratio | -16.2% | -18.1% (p=2e-21) | 4/4: email +68%, iOS×DE +44%, iOS×US (offset) -25%, 3-way +14% | none | 9 | 64s |
| `active_customers` | count-distinct | -5.1% | -5.2% (p=5e-15) | 1/1 (email): email +51% | channel=email & country=DE, channel=paid_search, channel=organic, channel=referral, channel=tiktok | 11 | 99s |
| `conversion_rate`, quiet week (Sep 18-24) | ratio | -0.7% | -0.2% (p=0.92) | none, correctly | none | 3 | 2s |

**Revenue** misses the offsetting iOS-US effect *by design of the statistics*. Raw revenue is
heavy-tailed, and that effect's unique contribution (about −12% of the anomaly, CI −33%..+8%) doesn't clear the
multiple-testing bar there. The report lists it under "considered and rejected" with its estimate
and CI. The same effect is significant in revenue per session, where the ratio damps the noise.

**`active_customers`** finds email as the main driver, then spreads the rest thinly over the other
channels. Count-distinct baselines are the four baseline weeks, not a holiday-aware forecast, so
the German holiday's dip is unexplained. In this synthetic data, customers also aren't tied to one
country or channel, which makes session-level segments heavily overlapping. See the limitations
below.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\generate_data.py          # ~3.4M sessions, planted incidents + decoys
.venv\Scripts\dbt build --profiles-dir .               # DuckDB warehouse + semantic manifest + dbt tests
.venv\Scripts\python -m rca.cli --metric conversion_rate --out reports\conversion_rate.md
```

```powershell
python -m rca.cli --metric buyer_rate                      # count-distinct ratio (HLL path)
python -m rca.cli --metric revenue --json                  # machine-readable summary for alerting bots
python -m rca.cli --metric conversion_rate --current-end 2026-09-24   # quiet week: no causes
python -m rca.cli --metric conversion_rate --screen-sample 0.2         # successive halving
python -m rca.cli --metric conversion_rate --no-forecast               # naive baseline, for comparison
python -m rca.cli --metric conversion_rate --mode exhaustive           # legacy v1 oracle (slow)
python -m pytest -q                      # 28 unit + 16 end-to-end tests (~6 min)
python -m pytest -q --runslow            # + the exhaustive oracle
```

Example reports, with the statement trace and the wrapped SQL, are in [`reports/`](reports/).

## Limitations

- **Dialects.** Only DuckDB is exercised by the tests. The Snowflake and BigQuery templates in
  `rca/planner.py` (date literals, `datediff`, `hash`, `listagg`) follow the same structure but
  have not been run against a live warehouse.
- **Count-distinct baselines** are the baseline weeks, not forecasts, and the exclusivity weighting
  is a first-order correction when segments share entities.
- **The HLL estimator** is the original estimator with linear counting. HLL++ bias tables are not
  implemented, so sets of ~40k-80k carry about 1% bias, in the same direction for actual and
  counterfactual.
- **Runtime.** Count-distinct metrics take 1-2 minutes locally because exact `COUNT(DISTINCT)` at
  entity grain dominates. Additive metrics take about 20-30 s, mostly in DuckDB.
- **Recall is bounded by the crawl.** A cause whose dimensions never become implicated, and whose
  cells are below the noise floor, won't be proposed. Attribution can only judge candidates it is
  given.

## Project layout

```
models/            dbt models + semantic models (31 dimensions; additive, ratio and count-distinct metrics)
rca/
  semantic_client  the only module that talks to MetricFlow: metadata, compile, execute
  planner          SlicePlanner: GROUPING SETS wrapping, bin-packing, flag cubes, SQL-side HLL registers
  crawl            staged crawl: screening, aliases, surplus z-scores, beam search, heredity closure
  forecast         robust seasonal regression, calendar, noise floor
  detect           naive vs expected vs anomaly
  shapley          counterfactual games (additive + sketch), nesting, LOO, Owen values
  sketch           HLL decode / merge / estimate
  pipeline         detect -> crawl -> flag cube -> attribute
  report, cli      Markdown + JSON output
  exhaustive*      v1 engine kept as an oracle
scripts/           synthetic data with planted incidents + decoys
tests/             unit tests (no warehouse) + end-to-end tests scored against ground truth
```
