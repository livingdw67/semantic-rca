"""SlicePlanner: turn logical slice requests into a few physical warehouse statements.

The search strategy asks for *logical* slices ("totals of conversion_rate by platform x country,
baseline vs current"). The planner turns many of them into one statement:

1. **Compile with MetricFlow.** ``mf.explain()`` produces the governed SQL at grain
   ``day x {union of the block's dimensions}``. MetricFlow owns joins, filters and metric logic.
2. **Wrap.** That SQL becomes a CTE. The wrapper buckets days into windows and rolls the result
   up into many ``GROUPING SETS`` in a single scan. It only applies ``SUM`` to components the
   manifest proved additive, ``COUNT(DISTINCT entity)`` to count-distinct components, and HLL
   register maxima for sketches -- never any business logic.
3. **Split.** Rows are routed back to their logical request by the ``GROUPING()`` bitmask.

Blocks are bin-packed so that (a) the inner CTE's estimated cardinality
``days x min(prod(card(dims)), rows_per_day)`` stays under ``cell_budget`` and (b) no statement
carries more than ``max_sets`` grouping sets (every input row is aggregated once per set).

Dialects: DuckDB is exercised by the test suite. The Snowflake and BigQuery templates follow the
same structure but have not been run against a live warehouse.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import pandas as pd

from rca.semantic_client import TIME_GROUP_BY, MetricShape, SemanticLayer

Segment = tuple[tuple[str, str], ...]  # (("session__platform", "ios"), ("session__country", "DE"))
CURRENT = 0  # window label of the current window; baseline windows are 1..n


# --------------------------------------------------------------------------- dialects
@dataclass(frozen=True)
class Dialect:
    name: str
    date: str  # date literal template
    day_diff: str  # days from {a} to {b}
    hash64: str  # unsigned-ish 64-bit hash of {x}
    string_agg: str  # comma-joined aggregation of {x}
    log2: str = "log2({x})"
    shift_right: str = "({x} >> {n})"
    bit_and: str = "({x} & {n})"


DIALECTS = {
    "duckdb": Dialect(
        "duckdb", "DATE '{d}'", "date_diff('day', cast({a} as date), {b})", "hash({x})", "string_agg({x}, ',')"
    ),
    "snowflake": Dialect(
        "snowflake", "'{d}'::date", "datediff('day', {a}::date, {b})", "abs(hash({x}))", "listagg({x}, ',')",
        shift_right="bitshiftright({x}, {n})", bit_and="bitand({x}, {n})",
    ),
    "bigquery": Dialect(
        "bigquery", "DATE '{d}'", "date_diff({b}, date({a}), DAY)", "abs(farm_fingerprint(cast({x} as string)))",
        "string_agg({x}, ',')", log2="log({x}, 2)",
    ),
}


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def predicate(segment: Segment) -> str:
    """SQL predicate on MetricFlow output columns (dunder names) for a segment."""
    return "(" + " and ".join(f"{d} = {_lit(v)}" for d, v in segment) + ")"


# --------------------------------------------------------------------------- windows
@dataclass(frozen=True)
class Windows:
    """Current vs baseline windows.

    Additive metrics: one current window and one baseline window of arbitrary length, compared
    as per-day averages. Count-distinct metrics: distinct counts do not scale with window length,
    so both sides use rolling 7-day buckets ending at ``current[1]``: bucket 0 is current and
    buckets 1..n are baseline weeks (compared as the per-week average).
    """

    baseline: tuple[dt.date, dt.date]
    current: tuple[dt.date, dt.date]
    weekly: bool = False

    @classmethod
    def ending(cls, end: dt.date, current_days: int, baseline_days: int, weekly: bool) -> "Windows":
        if weekly and (current_days != 7 or baseline_days % 7):
            raise ValueError("count-distinct metrics need a 7-day current window and whole baseline weeks")
        cur = (end - dt.timedelta(days=current_days - 1), end)
        base = (cur[0] - dt.timedelta(days=baseline_days), cur[0] - dt.timedelta(days=1))
        return cls(base, cur, weekly)

    @property
    def start(self) -> dt.date:
        return self.baseline[0]

    @property
    def end(self) -> dt.date:
        return self.current[1]

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def n_baseline(self) -> int:
        return ((self.baseline[1] - self.baseline[0]).days + 1) // 7 if self.weekly else 1

    @property
    def normalisers(self) -> tuple[int, int]:
        """(baseline, current) divisors that turn window totals into comparable averages."""
        if self.weekly:
            return self.n_baseline, 1
        return (self.baseline[1] - self.baseline[0]).days + 1, (self.current[1] - self.current[0]).days + 1


# --------------------------------------------------------------------------- planner
@dataclass
class Block:
    sets: list[frozenset[str]] = field(default_factory=list)

    @property
    def dims(self) -> list[str]:
        return sorted(set().union(*self.sets)) if self.sets else []


class SlicePlanner:
    def __init__(
        self,
        sl: SemanticLayer,
        shape: MetricShape,
        windows: Windows,
        max_sets: int = 48,
        cell_budget: int = 5_000_000,
        approx_distinct: bool = False,
        where: Sequence[str] = (),
        label: str = "",
    ) -> None:
        self.sl = sl
        self.shape = shape
        self.windows = windows
        self.max_sets = max_sets
        self.cell_budget = cell_budget
        self.approx_distinct = approx_distinct
        # MetricFlow where-filters applied to every statement (e.g. one half of a sample split)
        self.where = list(where)
        self.label = label
        self.dialect = DIALECTS.get(sl.dialect, DIALECTS["duckdb"])
        self.cardinality: dict[str, int] = {}
        self.rows_per_day: Optional[float] = None
        self._cache: dict[str, pd.DataFrame] = {}
        self.statements: list[dict] = []  # trace: one entry per physical statement

    # ------------------------------------------------------------------ helpers
    def _date(self, d: dt.date) -> str:
        return self.dialect.date.format(d=d.isoformat())

    def _window_expr(self) -> str:
        w = self.windows
        if w.weekly:
            diff = self.dialect.day_diff.format(a=TIME_GROUP_BY, b=self._date(w.end))
            return f"cast(floor({diff} / 7) as integer)"
        return f"case when {TIME_GROUP_BY} >= {self._date(w.current[0])} then {CURRENT} else 1 end"

    def _components(self) -> list[tuple[str, str]]:
        """[(metric component, output alias n|d)]"""
        out = [(self.shape.numerator, "n")]
        if self.shape.denominator:
            out.append((self.shape.denominator, "d"))
        return out

    def _agg(self, component: str) -> str:
        if self.shape.is_distinct(component):
            member = f"case when {component} > 0 then {self.shape.distinct_entity} end"
            # approx_count_distinct is opt-in: DuckDB's is far too coarse inside GROUPING SETS
            # (errors of 15-20% that flipped the sign of the total change in testing).
            return f"approx_count_distinct({member})" if self.approx_distinct else f"count(distinct {member})"
        return f"sum({component})"

    def _grain_extra(self) -> list[str]:
        return [self.shape.distinct_entity] if self.shape.has_distinct else []

    def _compile(self, metrics, group_by, start, end, where: Sequence[str] = ()) -> str:
        return self.sl.compile_sql(metrics, group_by, start, end, [*self.where, *where])

    def _run(self, sql: str, purpose: str, n_sets: int = 0) -> pd.DataFrame:
        key = hashlib.sha1(sql.encode()).hexdigest()
        if key not in self._cache:
            t0 = time.perf_counter()
            self._cache[key] = self.sl.execute(sql)
            purpose = f"{purpose} ({self.label})" if self.label else purpose
            self.statements.append({
                "purpose": purpose, "grouping_sets": n_sets, "rows": len(self._cache[key]),
                "seconds": round(time.perf_counter() - t0, 2), "sql": sql,
            })
        return self._cache[key]

    def _est_cells(self, dims: Iterable[str]) -> float:
        combos = math.prod(self.cardinality.get(d, 50) for d in dims)
        if self.rows_per_day:
            combos = min(combos, self.rows_per_day)
        return combos * self.windows.days

    # ------------------------------------------------------------------ stage 0
    def dimension_stats(self, dims: Sequence[str], primary_entity: Optional[str]) -> pd.DataFrame:
        """Cardinality and top-value share per dimension, plus rows/day, in ONE statement.

        Each branch is a narrow MetricFlow query (one dimension, no day grain), so even a
        user_id-like dimension costs one column scan and returns a single row.
        """
        vol = self.shape.denominator or self.shape.numerator
        branches = []
        for d in dims:
            mf = self._compile([vol], [d], self.windows.start, self.windows.end)
            branches.append(
                f"select {_lit(d)} as rca_dim, count(*) as rca_card, max(rca_v) as rca_top, sum(rca_v) as rca_tot "
                f"from (select {d}, {vol} as rca_v from ({mf}) rca_q) rca_t"
            )
        if primary_entity:
            mf = self._compile([vol], [TIME_GROUP_BY, primary_entity], self.windows.start, self.windows.end)
            branches.append(
                f"select '__rows__' as rca_dim, count(*) as rca_card, null as rca_top, null as rca_tot "
                f"from ({mf}) rca_q"
            )
        df = self._run("\nunion all\n".join(branches), "stage 0: dimension stats")
        df["rca_card"] = pd.to_numeric(df["rca_card"])
        rows = df[df["rca_dim"] == "__rows__"]
        if len(rows):
            self.rows_per_day = float(rows["rca_card"].iloc[0]) / self.windows.days
        df = df[df["rca_dim"] != "__rows__"].copy()
        df["top_share"] = pd.to_numeric(df["rca_top"]) / pd.to_numeric(df["rca_tot"]).replace(0, float("nan"))
        self.cardinality.update(dict(zip(df["rca_dim"], df["rca_card"].astype(int))))
        return df.rename(columns={"rca_dim": "dimension", "rca_card": "cardinality"})[
            ["dimension", "cardinality", "top_share"]
        ]

    # ------------------------------------------------------------------ stages 1-2
    def fetch(
        self, sets: Iterable[Iterable[str]], where: Sequence[str] = (), purpose: str = "slices"
    ) -> dict[frozenset[str], pd.DataFrame]:
        """Window totals for each requested grouping set: columns [*sorted(set), n0, n1(, d0, d1)]."""
        wanted = list(dict.fromkeys(frozenset(s) for s in sets))
        out: dict[frozenset[str], pd.DataFrame] = {}
        for block in self._pack(wanted):
            sql = self._wrap_grouping_sets(block, where)
            raw = self._run(sql, purpose, n_sets=len(block.sets))
            out |= self._split(raw, block)
        return out

    def _pack(self, sets: list[frozenset[str]]) -> list[Block]:
        """First-fit decreasing on estimated inner cardinality."""
        blocks: list[Block] = []
        for s in sorted(sets, key=lambda s: -self._est_cells(s)):
            for b in blocks:
                if len(b.sets) < self.max_sets and self._est_cells(set(b.dims) | s) <= self.cell_budget:
                    b.sets.append(s)
                    break
            else:
                blocks.append(Block([s]))
        return blocks

    def _wrap_grouping_sets(self, block: Block, where: Sequence[str]) -> str:
        dims = block.dims
        comps = self._components()
        mf = self._compile(
            self.shape.base_metrics, [TIME_GROUP_BY, *dims, *self._grain_extra()],
            self.windows.start, self.windows.end, where,
        )
        inner_cols = ", ".join([*dims, *self._grain_extra(), *[c for c, _ in comps]])
        grouping = ", ".join(f"grouping({d}) as rca_g{i}" for i, d in enumerate(dims))
        aggs = ", ".join(f"{self._agg(c)} as {alias}" for c, alias in comps)
        sets_sql = ",\n    ".join("(" + ", ".join(["rca_win", *sorted(s)]) + ")" for s in block.sets)
        return (
            f"with rca_mf as (\n{mf}\n),\n"
            f"rca_w as (select {self._window_expr()} as rca_win, {inner_cols} from rca_mf)\n"
            f"select rca_win, {grouping}, {', '.join(dims)}, {aggs}\n"
            f"from rca_w\n"
            f"where rca_win between {CURRENT} and {self.windows.n_baseline}\n"
            f"group by grouping sets (\n    {sets_sql}\n)"
        )

    def _split(self, raw: pd.DataFrame, block: Block) -> dict[frozenset[str], pd.DataFrame]:
        dims = block.dims
        gcols = [f"rca_g{i}" for i in range(len(dims))]
        masks = raw[gcols].astype(int).to_numpy() if gcols else None
        has_d = self.shape.denominator is not None
        out = {}
        for s in block.sets:
            want = [0 if d in s else 1 for d in dims]
            rows = raw[(masks == want).all(axis=1)] if gcols else raw
            keys = sorted(s)
            rows = rows.assign(**{k: rows[k].astype(str) for k in keys})
            cur = rows[rows["rca_win"].astype(int) == CURRENT]
            base = rows[rows["rca_win"].astype(int) != CURRENT]
            vals = ["n", "d"] if has_d else ["n"]
            agg0 = base.groupby(keys)[vals].sum().astype(float).rename(columns=lambda c: c + "0")
            agg1 = cur.groupby(keys)[vals].sum().astype(float).rename(columns=lambda c: c + "1")
            out[s] = agg0.join(agg1, how="outer").fillna(0.0).reset_index()
        return out

    # ------------------------------------------------------------------ stage 3
    def flag_cube(self, segments: Sequence[Segment], start: dt.date, end: dt.date) -> pd.DataFrame:
        """Daily additive components per flag cell (which candidate segments a row belongs to).

        Returns [day, f0..f{K-1}, n(, d)] with at most 2^K cells per day. Everything downstream
        -- peeling, exclusive attribution, Shapley, per-cell forecasts -- runs on this locally.
        """
        dims = sorted({d for seg in segments for d, _ in seg})
        mf = self._compile(self.shape.base_metrics, [TIME_GROUP_BY, *dims], start, end)
        flags = [f"case when {predicate(seg)} then 1 else 0 end as rca_f{i}" for i, seg in enumerate(segments)]
        comps = self._components()
        aggs = ", ".join(f"sum({c}) as {alias}" for c, alias in comps)
        group = ", ".join(str(i) for i in range(1, len(segments) + 2))
        sql = (
            f"with rca_mf as (\n{mf}\n)\n"
            f"select cast({TIME_GROUP_BY} as date) as rca_day, {', '.join(flags)}, {aggs}\n"
            f"from rca_mf group by {group}"
        )
        df = self._run(sql, "stage 3: flag cube (daily)")
        df["rca_day"] = pd.to_datetime(df["rca_day"]).dt.date
        for c in ("n", "d"):
            if c in df:
                df[c] = pd.to_numeric(df[c]).astype(float)
        return df

    def flag_sketches(self, segments: Sequence[Segment], precision: int = 14) -> pd.DataFrame:
        """HLL registers per (window bucket, flag cell, distinct component), sparse-encoded.

        Registers are computed in SQL from a 64-bit hash of the entity key:
        register = h mod 2^p, rho = leading zeros of (h >> p) + 1. Encoding them as
        "register:rho,..." strings keeps the result to one row per (bucket, cell, component).
        Registers merge with MAX, so any union of cells, and therefore any counterfactual in
        the Shapley stage, can be estimated client-side without another query.
        """
        if not self.shape.has_distinct:
            raise ValueError("flag_sketches is only for count-distinct components")
        p, d, w = precision, self.dialect, self.windows
        entity = self.shape.distinct_entity
        dims = sorted({dim for seg in segments for dim, _ in seg})
        mf = self._compile(self.shape.base_metrics, [TIME_GROUP_BY, *dims, entity], w.start, w.end)
        flags = ", ".join(f"case when {predicate(seg)} then 1 else 0 end as rca_f{i}" for i, seg in enumerate(segments))
        fcols = ", ".join(f"rca_f{i}" for i in range(len(segments)))
        h = d.hash64.format(x=entity)
        reg = d.bit_and.format(x="rca_h", n=(1 << p) - 1)
        tail = d.shift_right.format(x="rca_h", n=p)
        rho = (
            f"case when {tail} = 0 then {64 - p + 1} "
            f"else {64 - p} - cast(floor({d.log2.format(x=tail)}) as integer) end"
        )
        branches = []
        for comp, _ in self.shape.distinct:
            branches.append(
                f"select rca_win, {fcols}, {_lit(comp)} as rca_comp, {reg} as rca_reg, max({rho}) as rca_rho "
                f"from rca_w where {comp} > 0 group by rca_win, {fcols}, rca_comp, rca_reg"
            )
        encoded = d.string_agg.format(x="cast(rca_reg as varchar) || ':' || cast(rca_rho as varchar)")
        distinct_cols = ", ".join(c for c, _ in self.shape.distinct)
        sql = (
            f"with rca_mf as (\n{mf}\n),\n"
            f"rca_w as (select {self._window_expr()} as rca_win, {flags}, {h} as rca_h, {distinct_cols} from rca_mf),\n"
            "rca_r as (\n" + "\nunion all\n".join(branches) + "\n)\n"
            f"select rca_win, {fcols}, rca_comp, {encoded} as rca_regs\n"
            f"from rca_r where rca_win between {CURRENT} and {w.n_baseline}\n"
            f"group by rca_win, {fcols}, rca_comp"
        )
        return self._run(sql, "stage 3: flag sketches (HLL)")

    def flag_cube_weekly(self, segments: Sequence[Segment]) -> pd.DataFrame:
        """Additive components per (window bucket, flag cell) -- the count-distinct companion."""
        dims = sorted({d for seg in segments for d, _ in seg})
        w = self.windows
        mf = self._compile(self.shape.base_metrics, [TIME_GROUP_BY, *dims], w.start, w.end)
        flags = ", ".join(f"case when {predicate(seg)} then 1 else 0 end as rca_f{i}" for i, seg in enumerate(segments))
        additive = [(c, a) for c, a in self._components() if not self.shape.is_distinct(c)]
        aggs = ", ".join(f"sum({c}) as {a}" for c, a in additive) or "count(*) as rca_rows"
        group = ", ".join(str(i) for i in range(1, len(segments) + 2))
        sql = (
            f"with rca_mf as (\n{mf}\n)\n"
            f"select {self._window_expr()} as rca_win, {flags}, {aggs} from rca_mf group by {group}"
        )
        return self._run(sql, "stage 3: flag cube (weekly additive)")

    # ------------------------------------------------------------------ detection series
    def history(self, start: dt.date) -> pd.DataFrame:
        """Top-line components over [start, current end]: daily (additive) or weekly buckets."""
        w = self.windows
        if not self.shape.has_distinct:
            return self.sl.query(self.shape.base_metrics, start=start, end=w.end, where=self.where)
        mf = self._compile(self.shape.base_metrics, [TIME_GROUP_BY, self.shape.distinct_entity], start, w.end)
        diff = self.dialect.day_diff.format(a=TIME_GROUP_BY, b=self._date(w.end))
        approx, self.approx_distinct = self.approx_distinct, False  # detection needs exact counts
        aggs = ", ".join(f"{self._agg(c)} as {c}" for c in self.shape.base_metrics)
        self.approx_distinct = approx
        sql = (
            f"with rca_mf as (\n{mf}\n)\n"
            f"select cast(floor({diff} / 7) as integer) as rca_bucket, min(cast({TIME_GROUP_BY} as date)) as rca_start, "
            f"count(distinct cast({TIME_GROUP_BY} as date)) as rca_days, {aggs}\n"
            f"from rca_mf group by 1"
        )
        df = self._run(sql, "detection: weekly distinct history")
        for c in self.shape.base_metrics:
            df[c] = pd.to_numeric(df[c]).astype(float)
        df["rca_start"] = pd.to_datetime(df["rca_start"]).dt.date
        return df[pd.to_numeric(df["rca_days"]) == 7].sort_values("rca_start").reset_index(drop=True)
