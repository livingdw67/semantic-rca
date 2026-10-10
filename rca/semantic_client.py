"""Thin wrapper around the MetricFlow engine.

This is the only module that talks to the semantic layer. Everything the RCA engine knows about
the business -- which metrics exist, how a ratio decomposes, which dimensions can slice a metric
(including ones reachable only through joins) -- comes from here, never from hand-written SQL.
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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricShape:
    """How a metric decomposes into additive parts.

    simple: value = numerator            (summable across days and segments)
    ratio:  value = numerator / denominator, both summable
    """

    name: str
    label: str
    kind: str
    numerator: str
    denominator: Optional[str] = None

    @property
    def base_metrics(self) -> list[str]:
        return [self.numerator] + ([self.denominator] if self.denominator else [])


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
        """Resolve a governed metric into summable components using the manifest."""
        if name not in self._metrics:
            raise KeyError(f"Unknown metric {name!r}. Available: {', '.join(self.metric_names())}")
        metric = self._metrics[name]
        label = metric.label or name
        if metric.type is MetricType.SIMPLE:
            self._assert_additive(metric)
            return MetricShape(name=name, label=label, kind="simple", numerator=name)
        if metric.type is MetricType.RATIO:
            num = metric.type_params.numerator.name
            den = metric.type_params.denominator.name
            for part in (num, den):
                if self._metrics[part].type is not MetricType.SIMPLE:
                    raise ValueError(f"{name}: ratio component {part!r} must be a simple metric")
                self._assert_additive(self._metrics[part])
            return MetricShape(name=name, label=label, kind="ratio", numerator=num, denominator=den)
        raise ValueError(f"{name}: metric type {metric.type.value!r} is not supported for RCA")

    def _assert_additive(self, metric) -> None:
        agg = None
        params = getattr(metric.type_params, "metric_aggregation_params", None)
        if params is not None and getattr(params, "agg", None) is not None:
            agg = params.agg
        elif metric.type_params.measure is not None:
            measure = self._measures.get(metric.type_params.measure.name)
            agg = measure.agg if measure is not None else None
        if agg not in ADDITIVE_AGGS:
            raise ValueError(
                f"{metric.name}: aggregation {agg} is not additive, so it cannot be decomposed by segment"
            )

    def categorical_dimensions(self, metric: str) -> list[str]:
        """Every categorical dimension that can slice ``metric`` -- local or joined."""
        shape = self.metric_shape(metric)
        dims = self._mf.simple_dimensions_for_metrics(shape.base_metrics)
        return sorted(
            {d.dunder_name for d in dims if d.type is DimensionType.CATEGORICAL and not d.is_partition}
        )

    # ------------------------------------------------------------------ queries
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
