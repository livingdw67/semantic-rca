"""Staged crawl over the dimension lattice with a small, bounded number of statements.

Stage 0  screen      ONE statement: cardinality + top-value share for every dimension.
                     Drop ``meta: {rca: exclude}``, IDs (cardinality > max_card), near-constants.
Stage 1  marginals   every surviving 1-way marginal, typically ONE GROUPING SETS statement.
                     Collapse exact aliases (storefront == country) into one representative.
Stage 2  drill       beam search: expand the top-``beam`` nodes along every other dimension, a few
                     batched statements per level. A child replaces its parent only when it is a
                     *concentrated* explanation of the parent's driver (rate or mix effect).
                     Optional successive halving on a governed hash sample.
Stage 3  hand-off    top-k non-redundant segments go to attribution (one flag-cube statement).

Scoring. A segment's *surplus* is its share of the change minus its share of volume: a slice that
is 57% of traffic and carries 63% of the drop is unremarkable, because every cause spreads across
it in proportion to its size. Surplus is z-tested against the forecast's noise floor
(``Detection.contribution_se``), so random drift in big noise segments does not enter the beam.
For ratio metrics the mix effect is also tested on its own, so a traffic shift is not hidden by
the same segment's rate drifting the other way.

Why best-first and not a stochastic bandit: a warehouse query returns its exact answer the first
time, so there is no reward noise to average over. The bandit framing *is* right for screening on
a sample, where sampling noise is real -- that is what ``screen_sample`` does (successive halving).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Optional

import numpy as np
import pandas as pd

from rca.attribution import Cause, _row_to_cause, _volume_share, decompose
from rca.planner import SlicePlanner
from rca.preflight import screen_dimensions
from rca.semantic_client import SemanticLayer, pretty_dimension

RAW = ("n0", "n1", "d0", "d1")


@dataclass
class CrawlConfig:
    max_card: int = 500
    near_constant: float = 0.995
    min_surplus: float = 0.05  # |pct_of_change - share| for a node to enter the beam ...
    min_z: float = 3.0  # ... and its z against the noise floor
    score: str = "surplus"  # or "excess" (experimental, see Crawler._decompose)
    beam: int = 10
    max_depth: int = 3
    max_sets_per_level: int = 300
    top_k: int = 12  # recall: the attribution stage rejects the extras with reasons
    keep_ratio: float = 0.60
    min_lift: float = 1.5
    screen_sample: Optional[float] = None  # e.g. 0.10 -> successive halving on a 10% hash sample
    halving_eta: int = 3
    closure_dims: int = 5
    sample_dimension: str = "session__sample_bucket"


@dataclass
class Node:
    cause: Cause
    level: int
    parent: Optional["Node"] = None
    child: Optional["Node"] = None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(d for d, _ in self.cause.dims)

    def terminal(self) -> "Node":
        n = self
        while n.child is not None:
            n = n.child
        return n


@dataclass
class CrawlResult:
    dims: list[str]
    screened: dict[str, str]
    aliases: dict[str, list[str]]
    stats: pd.DataFrame
    tables: dict[frozenset[str], pd.DataFrame]
    candidates: list[Cause]
    levels: list[dict]
    statements: int
    total_change: float


def primary_entity(sl: SemanticLayer, metrics: list[str]) -> Optional[str]:
    for e in sl._mf.entities_for_metrics(metrics):
        if getattr(e.type, "value", str(e.type)) == "primary":
            return e.name
    return None


def describe_segment(dims) -> str:
    return " & ".join(f"{pretty_dimension(d)}={v}" for d, v in dims)


def _match(table: pd.DataFrame, values: dict[str, str]) -> pd.DataFrame:
    mask = np.ones(len(table), dtype=bool)
    for d, v in values.items():
        mask &= (table[d].astype(str) == v).to_numpy()
    return table[mask]


class Crawler:
    def __init__(
        self,
        sl: SemanticLayer,
        planner: SlicePlanner,
        config: CrawlConfig = CrawlConfig(),
        contribution_se: Optional[Callable[[np.ndarray], np.ndarray]] = None,
    ):
        self.sl, self.planner, self.cfg = sl, planner, config
        self.shape = planner.shape
        self.contribution_se = contribution_se
        self.tables: dict[frozenset[str], pd.DataFrame] = {}
        self.total_change = float("nan")
        self.preflight_notes: list[str] = []

    # ------------------------------------------------------------------ scoring
    def _decompose(self, raw: dict[frozenset[str], pd.DataFrame]) -> dict[frozenset[str], pd.DataFrame]:
        base, cur = self.planner.windows.normalisers
        out = {}
        for s, t in raw.items():
            df = decompose(t, sorted(s), self.shape.kind, base, cur)
            df["surplus"] = df["pct_of_change"] - df["share0"]
            # Experimental, CrawlConfig.score = "excess": how far the share of the change falls
            # outside [0, share of volume]. A segment in between simply did not move: when email
            # collapses, organic carries none of the drop without being a cause, and scoring it by
            # surplus fills the beam with such complements. Not the default: an offsetting segment
            # then scores |pct| instead of |pct| + share, and on the showcase the US-iOS win drops
            # out of the beam for orders, revenue and buyer_rate.
            if self.cfg.score == "excess":
                pct, share = df["pct_of_change"], df["share0"]
                df["beam_score"] = np.where(pct > share, pct - share, np.where(pct < 0, pct, 0.0))
            else:
                df["beam_score"] = df["surplus"]
            # Each table's own total: for count-distinct metrics, session-level dimensions overlap
            # (one customer, several screen sizes), so totals differ from table to table.
            total = float(df["contribution"].sum())
            if self.contribution_se is not None and total:
                se = self.contribution_se(df["share0"].to_numpy()) / abs(total)
                df["z"] = np.abs(df["beam_score"]) / np.maximum(se, 1e-12)
                if "mix_effect" in df:
                    # A traffic shift is a mechanism of its own, expected to be zero, so it is tested
                    # on its own too: email losing half its traffic while converting a little better
                    # by chance has a large mix effect but an unremarkable net surplus.
                    df["mix_share"] = df["mix_effect"].fillna(0.0) / total
                    df["z_mix"] = np.abs(df["mix_share"]) / np.maximum(se, 1e-12)
            else:
                df["z"] = np.inf
            out[s] = df
        return out

    def _hot(self, df: pd.DataFrame) -> pd.Series:
        hot = (df["beam_score"].abs() >= self.cfg.min_surplus) & (df["z"] >= self.cfg.min_z)
        if "z_mix" in df:
            hot |= (df["mix_share"].abs() >= self.cfg.min_surplus) & (df["z_mix"] >= self.cfg.min_z)
        return hot

    def _cause(self, row: pd.Series) -> Cause:
        c = _row_to_cause(row, self.shape.kind)
        c.score = float(row["surplus"])
        c.z = max(float(row["z"]), float(row.get("z_mix", 0.0)))
        c.raw = {k: float(row[k]) for k in RAW if k in row}
        return c

    def _canonical(self, c: Cause) -> Cause:
        """Drop dimensions implied by the others: country=DE & region=EU is just country=DE."""
        dims = list(c.dims)
        changed = True
        while changed and len(dims) > 1:
            changed = False
            for i in range(len(dims)):
                rest = dims[:i] + dims[i + 1:]
                table = self.tables.get(frozenset(d for d, _ in rest))
                if table is None:
                    continue
                m = _match(table, dict(rest))
                if len(m) == 1 and all(math.isclose(float(m.iloc[0][k]), c.raw[k], rel_tol=1e-9, abs_tol=1e-9)
                                       for k in c.raw):
                    dims, changed = rest, True
                    break
        c.dims = tuple(sorted(dims))
        return c

    def _driver_component(self, c: Cause) -> float:
        if self.shape.kind != "ratio" or np.isnan(c.rate_effect):
            return c.score
        return c.rate_effect if c.driver == "rate" else c.mix_effect

    # ------------------------------------------------------------------ refinement
    def _concentration(self, parent: Cause, child: Cause) -> Optional[float]:
        """Lift if ``child`` is a concentrated explanation of ``parent``'s driver, else None."""
        p_comp = self._driver_component(parent)
        p_vol = _volume_share(parent, self.shape.kind)
        if p_comp == 0 or p_vol <= 0 or child.driver != parent.driver:
            return None
        kept = self._driver_component(child) / p_comp
        lift = kept / max(_volume_share(child, self.shape.kind) / p_vol, 1e-9)
        return lift if kept >= self.cfg.keep_ratio and lift >= self.cfg.min_lift else None

    def _refine(self, node: Node) -> Optional[Node]:
        """Best concentrated child of ``node`` among the fetched tables.

        kept = child's driver component / parent's driver component  (>= keep_ratio)
        lift = kept / (child volume / parent volume)                 (>= min_lift)
        The driver component matters: email's *mix* effect is not concentrated anywhere, even
        though email & EU overlaps the iOS-DE *rate* collapse.
        """
        parent = node.cause
        p_comp = self._driver_component(parent)
        p_vol = _volume_share(parent, self.shape.kind)
        if p_comp == 0 or p_vol <= 0:
            return None
        best, best_lift = None, 0.0
        values = dict(parent.dims)
        for s, table in self.tables.items():
            if len(s) != len(node.keys) + 1 or not node.keys < s:
                continue
            kids = _match(table, values)
            kids = kids[self._hot(kids)]
            if kids.empty:
                continue
            comp_col = "score" if self.shape.kind != "ratio" else ("rate_effect" if parent.driver == "rate" else "mix_effect")
            comp = kids["surplus"] if comp_col == "score" else kids[comp_col]
            kids = kids.loc[(comp / p_comp).sort_values(ascending=False).index].head(3)
            for _, row in kids.iterrows():
                child = self._cause(row)
                lift = self._concentration(parent, child)
                if lift is not None and lift > best_lift:
                    best, best_lift = child, lift
        if best is None:
            return None
        best = self._canonical(best)
        if best.dims == parent.dims:
            return None
        best.refined_from = [parent.dims, *parent.refined_from]
        return Node(best, node.level + 1, parent=node)

    def _activity(self, dim: str) -> float:
        t = self.tables.get(frozenset({dim}))
        return float(t["surprise"].sum()) if t is not None else 0.0

    # ------------------------------------------------------------------ stages
    def run(self) -> CrawlResult:
        cfg, sl, planner = self.cfg, self.sl, self.planner
        q0 = sl.queries_run
        metric = self.shape.name

        # Stage 0: screen
        meta = sl.dimension_meta(metric)
        screened = {d: "meta rca: exclude" for d, m in meta.items() if m.get("rca") == "exclude"}
        dims = [d for d in meta if d not in screened]
        stats = planner.dimension_stats(dims, primary_entity(sl, self.shape.base_metrics))
        for _, r in stats.iterrows():
            if r["cardinality"] > cfg.max_card:
                screened[r["dimension"]] = f"cardinality {int(r['cardinality']):,} > {cfg.max_card}"
            elif r["cardinality"] <= 1 or (pd.notna(r["top_share"]) and r["top_share"] >= cfg.near_constant):
                screened[r["dimension"]] = (f"one value carries {r['top_share']:.2%} of the metric: near-constant, "
                                            "or derived from the outcome")
        dims = sorted(d for d in dims if d not in screened)

        # Stage 1: marginals
        raw = planner.fetch([{d} for d in dims], purpose="stage 1: marginals")
        first = decompose(next(iter(raw.values())), sorted(next(iter(raw))), self.shape.kind,
                          *planner.windows.normalisers)
        self.total_change = float(first["contribution"].sum())
        self.tables |= self._decompose(raw)
        aliases = self._collapse_aliases(dims)
        dims = sorted(aliases)
        # Pre-flight: renamed categories and outcome-derived dimensions (rca/preflight.py).
        check = screen_dimensions(self.tables, dims, self.shape.kind, meta)
        for d, reason in check.excluded.items():
            screened[d] = reason
            self.tables.pop(frozenset({d}), None)
        dims = [d for d in dims if d not in check.excluded]
        self.preflight_notes = check.notes
        levels = [{"level": 1, "sets": len(dims), "statements": sl.queries_run - q0}]

        nodes1 = []
        for d in dims:
            t = self.tables[frozenset({d})]
            for _, row in t[self._hot(t)].iterrows():
                nodes1.append(Node(self._cause(row), 1))
        beam = sorted(nodes1, key=lambda n: -n.cause.z)[: cfg.beam]
        all_nodes = list(nodes1)
        levels[0]["hot"] = len(nodes1)

        # Stage 2: beam search down the lattice
        for level in range(2, cfg.max_depth + 1):
            if not beam:
                break
            q_level = sl.queries_run
            priors: dict[frozenset[str], float] = {}
            for node in beam:
                for d in dims:
                    s = node.keys | {d}
                    if d not in node.keys and s not in self.tables:
                        priors[s] = max(priors.get(s, 0.0), node.cause.z * (self._activity(d) + 1e-4))
            sets = sorted(priors, key=lambda s: -priors[s])[: cfg.max_sets_per_level]
            pruned = len(priors) - len(sets)
            sampled = 0
            if cfg.screen_sample and len(sets) > cfg.halving_eta:
                sampled = len(sets)
                sets = self._halve(sets, beam)
            self.tables |= self._decompose(planner.fetch(sets, purpose=f"stage 2: level {level}"))

            next_beam = []
            for node in beam:
                child = self._refine(node)
                if child is not None:
                    node.child = child
                    next_beam.append(child)
                    all_nodes.append(child)
            levels.append({
                "level": level, "sets": len(sets), "pruned_by_prior": pruned, "screened_on_sample": sampled,
                "statements": sl.queries_run - q_level,
                "refined": [(describe_segment(n.parent.cause.dims), describe_segment(n.cause.dims)) for n in next_beam],
            })
            uniq = {n.cause.dims: n for n in next_beam}
            beam = sorted(uniq.values(), key=lambda n: -n.cause.z)[: cfg.beam]

        # Stage 2b: effect heredity. Interactions are most likely among dimensions that already
        # show effects, so fetch the dense sub-lattice over the implicated dimensions. This is
        # what finds a 3-way cause whose 1- and 2-way marginals are masked by noise or offsets.
        q_closure = sl.queries_run
        implicated: dict[str, float] = {}
        for n in all_nodes:
            for d, _ in n.cause.dims:
                implicated[d] = max(implicated.get(d, 0.0), n.cause.z)
        top = sorted(implicated, key=lambda d: -implicated[d])[: cfg.closure_dims]
        closure = [frozenset(c) for k in range(2, cfg.max_depth + 1) for c in combinations(sorted(top), k)]
        closure = [c for c in closure if c not in self.tables]
        if closure:
            self.tables |= self._decompose(planner.fetch(closure, purpose="stage 2b: heredity closure"))
        harvested = self._harvest(all_nodes)
        levels.append({"level": "closure", "dims": [pretty_dimension(d) for d in top], "sets": len(closure),
                       "statements": sl.queries_run - q_closure, "harvested": len(harvested)})

        candidates = self._select(all_nodes + harvested)
        return CrawlResult(dims, screened, aliases, stats, self.tables, candidates, levels,
                           sl.queries_run - q0, self.total_change)

    def _harvest(self, existing: list[Node]) -> list[Node]:
        """Significant cells from every fetched multi-dimension table, minus mere slices.

        A cell is a slice (and dropped) when one of its parents is itself significant in the same
        direction and the cell is not a concentrated explanation of it: DE & organic is just a
        slice of DE, while DE & iOS concentrates DE's drop and is kept.
        """
        known = {n.cause.dims for n in existing}
        out = []
        for s, table in self.tables.items():
            if len(s) < 2:
                continue
            for _, row in table[self._hot(table)].iterrows():
                cell = self._cause(row)
                if cell.dims in known:
                    continue
                if self._is_slice(cell):
                    continue
                cell = self._canonical(cell)
                if cell.dims in known:
                    continue
                known.add(cell.dims)
                out.append(Node(cell, len(cell.dims)))
        return out

    def _is_slice(self, cell: Cause) -> bool:
        dims = list(cell.dims)
        for i in range(len(dims)):
            rest = dims[:i] + dims[i + 1:]
            table = self.tables.get(frozenset(d for d, _ in rest))
            if table is None:
                continue
            m = _match(table, dict(rest))
            if len(m) != 1 or not bool(self._hot(m).iloc[0]):
                continue
            parent = self._cause(m.iloc[0])
            if math.copysign(1, parent.score) == math.copysign(1, cell.score) and                     self._concentration(parent, cell) is None:
                return True
        return False

    def _halve(self, sets: list[frozenset[str]], beam: list[Node]) -> list[frozenset[str]]:
        """Successive halving: score arms on a hash sample, keep the best 1/eta at full fidelity."""
        cfg = self.cfg
        cutoff = int(round(cfg.screen_sample * 1000))
        where = [f"{{{{ Dimension('{cfg.sample_dimension}') }}}} < {cutoff}"]
        saved, self.contribution_se = self.contribution_se, None  # z is meaningless on a sample
        sample = self._decompose(self.planner.fetch(sets, where=where, purpose="stage 2: sample screen"))
        self.contribution_se = saved
        reward = {}
        for s, table in sample.items():
            best = 0.0
            for node in beam:
                if node.keys < s:
                    kids = _match(table, dict(node.cause.dims))
                    if len(kids):
                        best = max(best, float(kids["beam_score"].abs().max()))
            reward[s] = best
        ranked = sorted(sets, key=lambda s: -reward[s])
        keep = max(1, math.ceil(len(ranked) / cfg.halving_eta))
        top = reward[ranked[0]] if ranked else 0.0
        return [s for i, s in enumerate(ranked) if i < keep or reward[s] >= 0.5 * top]

    def _collapse_aliases(self, dims: list[str]) -> dict[str, list[str]]:
        """Dimensions whose marginals are identical value-for-value are the same partition."""
        groups: dict[tuple, list[str]] = {}
        for d in dims:
            t = self.tables[frozenset({d})]
            cols = [c for c in RAW if c in t]
            sig = (len(t), tuple(sorted(tuple(round(float(x), 6) for x in r) for r in t[cols].to_numpy())))
            groups.setdefault(sig, []).append(d)
        out = {}
        for members in groups.values():
            rep, *rest = sorted(members)
            out[rep] = rest
            for alias in rest:
                self.tables.pop(frozenset({alias}), None)
        return out

    def _select(self, nodes: list[Node]) -> list[Cause]:
        """Terminal nodes, de-duplicated by support: segments with identical totals are the same
        rows described two ways (region=NA vs country=US) -- keep the shorter description and
        record the other as an equivalent."""
        by_support: dict[tuple, Cause] = {}
        for c in dict.fromkeys(n.terminal().cause.dims for n in nodes):
            c = next(n.terminal().cause for n in nodes if n.terminal().cause.dims == c)
            sig = tuple(round(c.raw.get(k, 0.0), 6) for k in RAW)
            if sig in by_support:
                keep = by_support[sig]
                if (len(c.dims), c.dims) < (len(keep.dims), keep.dims):
                    c.equivalents = [keep.dims, *keep.equivalents]
                    c.refined_from = list(dict.fromkeys(c.refined_from + keep.refined_from))
                    by_support[sig] = c
                elif c.dims != keep.dims and c.dims not in keep.equivalents:
                    keep.equivalents.append(c.dims)
            else:
                by_support[sig] = c
        pool = list(by_support.values())
        replaced = set()
        for a in pool:
            for b in pool:
                if a is not b and set(a.dims) < set(b.dims) and                         math.copysign(1, a.score) == math.copysign(1, b.score) and                         self._concentration(a, b) is not None:
                    replaced.add(a.dims)
                    b.refined_from = list(dict.fromkeys([a.dims, *b.refined_from]))
        ranked = sorted((c for c in pool if c.dims not in replaced), key=lambda c: -c.z)
        return ranked[: self.cfg.top_k]
