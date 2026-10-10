"""Thin wrapper around the MetricFlow engine.

This is the only module that talks to the semantic layer. Everything the RCA engine knows about
the business -- which metrics exist, how a ratio decomposes, which dimensions can slice a metric
(including ones reachable only through joins) -- comes from here, never from hand-written SQL.

Two execution paths share one statement counter:

* ``query()``    MetricFlow end to end (used by the exhaustive oracle and golden tests).
* ``execute()``  SQL that MetricFlow compiled (``compile_sql()``) and the planner wrapped with
                 GROUPING SETS / window buckets / sketches.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd
from dbt_metricflow.cli.cli_configuration import CLIConfiguration
from metricflow.engine.metricflow_engine import MetricFlowQueryRequest
from metricflow_semantic_interfaces.type_enums import AggregationType, DimensionType, MetricType

TIME_GROUP_BY = "metric_time__day"
ADDITIVE_AGGS = {AggregationType.SUM, AggregationType.COUNT, AggregationType.SUM_BOOLEAN}
DISTINCT_AGGS = {AggregationType.COUNT_DISTINCT}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricShape:
    """How a metric decomposes into components the engine can aggregate.

    simple: value = numerator
    ratio:  value = numerator / denominator

    Each component is either additive (sum/count: summable across days and segments) or
    distinct (count-distinct of an entity key: NOT summable, handled with HLL sketches).
    ``distinct`` maps each distinct component to the entity whose key it counts.
    """

    name: str
    label: str
    kind: str
    numerator: str
    denominator: Optional[str] = None
    distinct: tuple[tuple[str, str], ...] = ()

    @property
    def base_metrics(self) -> list[str]:
        return [self.numerator] + ([self.denominator] if self.denominator else [])

    @property
    def has_distinct(self) -> bool:
        return bool(self.distinct)

    def is_distinct(self, component: str) -> bool:
        return component in dict(self.distinct)

    @property
    def distinct_entity(self) -> Optional[str]:
        entities = {e for _, e in self.distinct}
        if len(entities) > 1:
            raise ValueError(f"{self.name}: components count different entities {sorted(entities)}")
        return next(iter(entities), None)


@dataclass
class SemanticLayer:
    project_dir: Path
    queries_run: int = 0
    sql_log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir).resolve()
        cfg = CLIConfiguration()
        cfg.setup(
            dbt_profiles_path=self.project_dir,
            dbt_project_path=self.project_dir,
            configure_file_logging=False,
        )
        self._cfg = cfg
        self._mf = cfg.mf
        self._metrics = {m.name: m for m in cfg.semantic_manifest.metrics}
        self._measures = {
            meas.name: meas for sm in cfg.semantic_manifest.semantic_models for meas in sm.measures
        }

    # ----------------------------------------------------------------- metadata
    def metric_names(self) -> list[str]:
        return sorted(self._metrics)

    def metric_shape(self, name: str) -> MetricShape:
        """Resolve a governed metric into aggregatable components using the manifest."""
        if name not in self._metrics:
            raise KeyError(f"Unknown metric {name!r}. Available: {', '.join(self.metric_names())}")
        metric = self._metrics[name]
        label = metric.label or name
        if metric.type is MetricType.SIMPLE:
            distinct = self._component_kind(metric)
            return MetricShape(name=name, label=label, kind="simple", numerator=name, distinct=distinct)
        if metric.type is MetricType.RATIO:
            num = metric.type_params.numerator.name
            den = metric.type_params.denominator.name
            distinct: tuple = ()
            for part in (num, den):
                if self._metrics[part].type is not MetricType.SIMPLE:
                    raise ValueError(f"{name}: ratio component {part!r} must be a simple metric")
                distinct += self._component_kind(self._metrics[part])
            shape = MetricShape(name=name, label=label, kind="ratio", numerator=num, denominator=den,
                                distinct=distinct)
            shape.distinct_entity  # noqa: B018 -- validates a single entity
            return shape
        raise ValueError(f"{name}: metric type {metric.type.value!r} is not supported for RCA")

    def _aggregation(self, metric):
        params = getattr(metric.type_params, "metric_aggregation_params", None)
        if params is not None and getattr(params, "agg", None) is not None:
            return params.agg
        if metric.type_params.measure is not None:
            measure = self._measures.get(metric.type_params.measure.name)
            return measure.agg if measure is not None else None
        return None

    def _component_kind(self, metric) -> tuple[tuple[str, str], ...]:
        """() for an additive component, ((name, entity),) for a sketchable count-distinct one."""
        agg = self._aggregation(metric)
        if agg in ADDITIVE_AGGS:
            return ()
        if agg in DISTINCT_AGGS:
            entity = (self.metric_meta(metric.name).get("rca") or {}).get("distinct_entity")
            if not entity:
                raise ValueError(
                    f"{metric.name}: count_distinct needs `config.meta.rca.distinct_entity` naming the entity "
                    "whose key it counts, so the engine can build mergeable sketches"
                )
            return ((metric.name, entity),)
        raise ValueError(
            f"{metric.name}: aggregation {agg} is neither additive nor count-distinct, so it cannot be "
            "decomposed by segment"
        )

    def metric_meta(self, name: str) -> dict:
        config = getattr(self._metrics[name], "config", None)
        return dict(getattr(config, "meta", None) or {})

    def categorical_dimensions(self, metric: str) -> list[str]:
        """Every categorical dimension that can slice ``metric`` (local or joined), minus the ones
        tagged ``config.meta.rca: exclude``."""
        return sorted(d for d, meta in self.dimension_meta(metric).items() if meta.get("rca") != "exclude")

    def dimension_meta(self, metric: str) -> dict[str, dict]:
        """Categorical dimension -> its ``config.meta`` (e.g. {"rca": "exclude"})."""
        shape = self.metric_shape(metric)
        out = {}
        for d in self._mf.simple_dimensions_for_metrics(shape.base_metrics):
            if d.type is not DimensionType.CATEGORICAL or d.is_partition:
                continue
            meta = getattr(d.config, "meta", None) if d.config is not None else None
            out[d.dunder_name] = dict(meta or {})
        return out

    @property
    def dialect(self) -> str:
        return self._cfg.dbt_project_metadata.profile.credentials.type

    # ------------------------------------------------------------------ compile / execute
    def compile_sql(
        self,
        metrics: Sequence[str],
        group_by: Sequence[str],
        start: Optional[dt.date] = None,
        end: Optional[dt.date] = None,
        where: Sequence[str] = (),
    ) -> str:
        """MetricFlow-compiled SQL for a request, without running it (local, ~15 ms)."""
        request = MetricFlowQueryRequest.create(
            metric_names=list(metrics),
            group_by_names=list(group_by) or None,
            time_constraint_start=_to_datetime(start),
            time_constraint_end=_to_datetime(end),
            where_constraints=list(where) or None,
        )
        return self._mf.explain(request).sql_statement.sql

    def execute(self, sql: str) -> pd.DataFrame:
        """Run one statement through MetricFlow's adapter-backed SQL client."""
        table = self._cfg.sql_client.query(sql)
        self.queries_run += 1
        self.sql_log.append(sql)
        return pd.DataFrame(list(table.rows), columns=list(table.column_names))

    def query(
        self,
        metrics: Sequence[str],
        group_by: Sequence[str] = (),
        start: Optional[dt.date] = None,
        end: Optional[dt.date] = None,
        by_day: bool = True,
        where: Sequence[str] = (),
    ) -> pd.DataFrame:
        group_by = ([TIME_GROUP_BY] if by_day else []) + list(group_by)
        request = MetricFlowQueryRequest.create(
            metric_names=list(metrics),
            group_by_names=group_by or None,
            time_constraint_start=_to_datetime(start),
            time_constraint_end=_to_datetime(end),
            where_constraints=list(where) or None,
        )
        result = self._mf.query(request)
        self.queries_run += 1
        self.sql_log.append(result.sql)
        table = result.result_df
        df = pd.DataFrame(list(table.rows), columns=list(table.column_names))
        if TIME_GROUP_BY in df:
            df[TIME_GROUP_BY] = pd.to_datetime(df[TIME_GROUP_BY]).dt.date
        for m in metrics:
            df[m] = pd.to_numeric(df[m]).fillna(0.0).astype(float)
        return df


def segment_filter(dims: Sequence[tuple[str, str]]) -> str:
    """Render a segment as a MetricFlow where clause, e.g. platform=ios AND country=DE."""
    parts = []
    for dim, value in dims:
        escaped = str(value).replace("'", "''")
        parts.append(f"{{{{ Dimension('{dim}') }}}} = '{escaped}'")
    return "(" + " AND ".join(parts) + ")"


def _to_datetime(d: Optional[dt.date]) -> Optional[dt.datetime]:
    if d is None:
        return None
    return dt.datetime(d.year, d.month, d.day)


def pretty_dimension(dunder: str) -> str:
    """'session__platform' -> 'platform'; 'customer__customer_tier' -> 'customer_tier'."""
    return dunder.split("__")[-1]
