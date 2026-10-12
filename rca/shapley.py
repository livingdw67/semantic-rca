"""Joint attribution over candidate segments: counterfactual games and Owen values.

The old engine peeled causes greedily: take the best segment, exclude it with a WHERE clause,
re-crawl. Whichever correlated segment was picked first absorbed all the shared credit, and the
answer depended on the order. Here all candidates are evaluated jointly on the flag cube.

The game
--------
Cells are the distinct combinations of candidate flags (at most 2^K). Each cell has current
totals and *expected* totals (from the per-cell forecast, or the naive baseline). A coalition of
players "reverts" the cells its segments touch to what was expected; the value of the coalition
is how much of the anomaly that reverting removes:

    v(S) = M(actual) - M(cells touched by S reverted)

Players per candidate:
  ratio metrics (additive parts)  a *rate* player and a *volume* player:
        rate revert   n -> expected_rate * d        (keep traffic, restore conversion)
        volume revert d -> expected_d, n -> rate * expected_d   (keep conversion, restore traffic)
  simple metrics                  one *level* player: n -> expected_n
  count-distinct metrics          one *membership* player: the cells' sketches -> baseline sketches

Credit
------
**Owen value** with one union per candidate: the candidate gets its Shapley value in the game
between candidates, and that is split fairly between its rate and volume players. Shapley is the
unique attribution that is efficient (credits sum to v(all)), symmetric, and gives nothing to a
player that never changes any coalition's value -- so overlapping candidates share the overlap
instead of the first one taking it all.

Shapley alone over-credits *supersets* (reverting "mobile" also reverts most of "iOS & DE"), so
the candidate set is cleaned first:

1. Equivalent supports (Jaccard >= 0.95) merge into one player: "country=US = region=NA".
2. Nesting: when A is inside B, compare the per-volume effect of B minus A against A's.
   ~0 -> B is a loose superset, keep the specific A.  ~1 -> A is just a slice of a homogeneous
   B, keep the general B.  Otherwise both are real and both stay.
3. Backward elimination: repeatedly drop the candidate with the smallest leave-one-out
   contribution v(all) - v(all minus it) while it is below ``min_pct`` of the anomaly or its
   bootstrap CI covers zero.

Uncertainty: day-level bootstrap of the current window plus forecast noise on the expected
values (additive), or the spread across baseline weeks (count-distinct).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Optional, Sequence

import numpy as np
from scipy import stats

from rca.sketch import estimate, merge


# --------------------------------------------------------------------------- games
class AdditiveGame:
    """Counterfactual game on additive components.

    flags (C x K) bool, n1/d1 current totals (C,), nh/dh expected totals (C,).
    ``per``: divisor for simple metrics (days in the current window) to report per-day levels.
    """

    def __init__(self, kind: str, flags: np.ndarray, n1, d1, nh, dh, per: float = 1.0):
        self.kind = kind
        self.flags = flags.astype(bool)
        self.K = flags.shape[1]
        self.effects = ("rate", "volume") if kind == "ratio" else ("level",)
        self.per = per
        self.set_data(n1, d1, nh, dh)

    def set_data(self, n1, d1, nh, dh) -> None:
        self.n1, self.nh = np.asarray(n1, float), np.asarray(nh, float)
        self.d1 = np.asarray(d1, float) if d1 is not None else np.ones_like(self.n1)
        self.dh = np.asarray(dh, float) if dh is not None else np.ones_like(self.nh)
        with np.errstate(divide="ignore", invalid="ignore"):
            r1 = self.n1 / self.d1
            rh = self.nh / self.dh
        self.r1 = np.where(np.isfinite(r1), r1, np.where(np.isfinite(rh), rh, 0.0))
        self.rh = np.where(np.isfinite(rh), rh, self.r1)

    @property
    def n_players(self) -> int:
        return self.K * len(self.effects)

    def player(self, k: int, effect: str) -> int:
        return k * len(self.effects) + self.effects.index(effect)

    def _metric(self, n: np.ndarray, d: np.ndarray) -> np.ndarray:
        if self.kind == "ratio":
            return n.sum(axis=-1) / d.sum(axis=-1)
        return n.sum(axis=-1) / self.per

    def actual(self) -> float:
        return float(self._metric(self.n1, self.d1))

    def expected(self) -> float:
        return float(self._metric(self.nh, self.dh))

    def anomaly(self) -> float:
        """The whole change vs expectation, including cells no candidate touches."""
        return self.actual() - self.expected()

    def value(self, coalitions: np.ndarray) -> np.ndarray:
        """coalitions (B x P) bool -> v (B,)"""
        E = len(self.effects)
        F = self.flags.astype(np.int32).T  # (K x C)
        touched = [(coalitions[:, e::E].astype(np.int32) @ F) > 0 for e in range(E)]  # each (B x C)
        if self.kind == "ratio":
            tr, tv = touched
            n = np.where(tr & tv, self.nh, np.where(tr, self.rh * self.d1, np.where(tv, self.r1 * self.dh, self.n1)))
            d = np.where(tv, self.dh, self.d1)
        else:
            n = np.where(touched[0], self.nh, self.n1)
            d = np.ones_like(n)
        return self.actual() - self._metric(n, d)

    def volume(self) -> np.ndarray:
        """Per-cell volume used for supports (expected denominator, or expected level)."""
        return self.dh if self.kind == "ratio" else np.abs(self.nh)


class SketchGame:
    """Counterfactual game for count-distinct components using HLL registers.

    comps: {"n": spec, "d": spec}; spec = ("additive", cur (C,), base (W x C)) or
                                          ("distinct", cur (C x m), base (W x C x m))
    v is averaged over the W baseline weeks; each week is one draw of "normal".
    """

    effects = ("membership",)

    def __init__(self, kind: str, flags: np.ndarray, comps: dict, precision: int):
        self.kind = kind
        self.flags = flags.astype(bool)
        self.K = flags.shape[1]
        self.comps = comps
        self.p = precision
        self.W = next(iter(comps.values()))[2].shape[0]

    n_players = property(lambda self: self.K)

    def player(self, k: int, effect: str = "membership") -> int:
        return k

    def _union(self, name: str, week: int, mask: np.ndarray) -> float:
        """Distinct-count estimate of the union of the cells in ``mask`` for one component and
        week (-1 = current). Memoised on (component, week, mask) -- never on id() of an array
        view, which Python reuses."""
        if not mask.any():
            return 0.0
        key = (name, week, mask.tobytes())
        cache = self.__dict__.setdefault("_union_cache", {})
        if key not in cache:
            _, cur, base = self.comps[name]
            regs = cur if week < 0 else base[week]
            cache[key] = estimate(merge(regs[mask]), self.p)
        return cache[key]

    def _component(self, name: str, touched: np.ndarray, w: int) -> float:
        """Counterfactual distinct count with the touched cells reverted to baseline week w.

        Never merges sketches from different weeks: the union of "current non-touched" and
        "baseline touched" would count customers active in either week and inflate. Instead,
        the touched region T changes the total by its own change in distinct count, weighted
        by its exclusivity -- the share of T's entities not present in any other cell:

            cf = |all|_cur - (|T|_cur - |T|_base) * excl(T),
            excl(T) = (|all| - |all minus T|) / |T|, averaged over the two weeks.

        Exact when segments never share entities (customer-level dimensions) and when T is
        everything; a first-order correction when they do (session-level dimensions).
        """
        kind, cur, base = self.comps[name]
        if kind == "additive":
            return float(np.where(touched, base[w], cur).sum())
        everything = np.ones(len(touched), bool)
        all_cur = self._union(name, -1, everything)
        if not touched.any():
            return all_cur
        if touched.all():
            return self._union(name, w, everything)
        t_cur, t_base = self._union(name, -1, touched), self._union(name, w, touched)
        excl_cur = (all_cur - self._union(name, -1, ~touched)) / t_cur if t_cur else 1.0
        all_base = self._union(name, w, everything)
        excl_base = (all_base - self._union(name, w, ~touched)) / t_base if t_base else 1.0
        excl = float(np.clip((excl_cur + excl_base) / 2, 0.0, 1.0))
        return all_cur - (t_cur - t_base) * excl

    def _metric(self, touched: np.ndarray, w: Optional[int]) -> float:
        def comp(name):
            spec = self.comps[name]
            if w is None:  # actual
                kind, cur, _ = spec
                return float(cur.sum()) if kind == "additive" else estimate(merge(cur), self.p)
            return self._component(name, touched, w)

        if self.kind == "ratio":
            return comp("n") / comp("d")
        return comp("n")

    def actual(self) -> float:
        return self._metric(np.zeros(len(self.flags), bool), None)

    def expected_by_week(self) -> np.ndarray:
        allc = np.ones(len(self.flags), bool)
        return np.array([self._metric(allc, w) for w in range(self.W)])

    def expected(self) -> float:
        return float(self.expected_by_week().mean())

    def anomaly(self) -> float:
        return self.actual() - self.expected()

    def value_by_week(self, coalitions: np.ndarray) -> np.ndarray:
        """(W x B)"""
        F = self.flags.astype(np.int32).T
        touched = (coalitions.astype(np.int32) @ F) > 0
        act = self.actual()
        out = np.empty((self.W, len(coalitions)))
        for w in range(self.W):
            for b in range(len(coalitions)):
                out[w, b] = act - self._metric(touched[b], w)
        return out

    def value(self, coalitions: np.ndarray) -> np.ndarray:
        return self.value_by_week(coalitions).mean(axis=0)

    def volume(self) -> np.ndarray:
        spec = self.comps.get("d", self.comps["n"])
        kind, cur, _ = spec
        if kind == "additive":
            return cur
        return np.array([estimate(cur[c][None, :], self.p) for c in range(len(cur))])


# --------------------------------------------------------------------------- Owen values
def owen_terms(unions: Sequence[Sequence[int]], n_players: int):
    """Coalition matrix and (player, weight, with-index, without-index) terms for exact Owen values."""
    index: dict[frozenset, int] = {}

    def idx(players: frozenset) -> int:
        if players not in index:
            index[players] = len(index)
        return index[players]

    m = len(unions)
    terms = []
    for k, U in enumerate(unions):
        others = [j for j in range(m) if j != k]
        for t in range(len(others) + 1):
            wT = math.factorial(t) * math.factorial(m - t - 1) / math.factorial(m)
            for T in combinations(others, t):
                base = frozenset(p for j in T for p in unions[j])
                for i in U:
                    rest = [p for p in U if p != i]
                    for s in range(len(rest) + 1):
                        wS = math.factorial(s) * math.factorial(len(U) - s - 1) / math.factorial(len(U))
                        for S in combinations(rest, s):
                            without = base | frozenset(S)
                            terms.append((i, wT * wS, idx(without | {i}), idx(without)))
    C = np.zeros((len(index), n_players), bool)
    for players, r in index.items():
        C[r, list(players)] = True
    t = np.array(terms, dtype=float)
    return C, t


def owen_from_values(V: np.ndarray, terms: np.ndarray, n_players: int) -> np.ndarray:
    """V (..., n_coalitions) -> phi (..., n_players)"""
    player = terms[:, 0].astype(int)
    contrib = terms[:, 1] * (V[..., terms[:, 2].astype(int)] - V[..., terms[:, 3].astype(int)])
    out = np.zeros(V.shape[:-1] + (n_players,))
    for p in range(n_players):
        out[..., p] = contrib[..., player == p].sum(axis=-1)
    return out


# --------------------------------------------------------------------------- driver
@dataclass
class Candidate:
    label: str
    segments: list  # one or more equivalent segment descriptions
    column: int  # flag column in the game
    status: str = "accepted"
    reason: str = ""
    phi: float = 0.0  # Owen value, metric units (sign of the anomaly when it explains it)
    phi_by_effect: dict = field(default_factory=dict)
    ci: tuple = (float("nan"), float("nan"))
    loo: float = float("nan")
    share_of_volume: float = 0.0

    def driver(self) -> str:
        if not self.phi_by_effect:
            return "level"
        if set(self.phi_by_effect) == {"rate", "volume"}:
            return "rate" if abs(self.phi_by_effect["rate"]) >= abs(self.phi_by_effect["volume"]) else "mix"
        return next(iter(self.phi_by_effect))


@dataclass
class Attribution:
    candidates: list[Candidate]
    actual: float
    expected: float
    anomaly: float
    explained: float  # v(all accepted)
    residual: float
    residual_ci: tuple
    log: list[str]

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status == "accepted"]


def _coalition(players: Sequence[int], n: int) -> np.ndarray:
    row = np.zeros((1, n), bool)
    row[0, list(players)] = True
    return row


def attribute(
    game,
    labels: list[str],
    resample: Optional[Callable[[np.random.Generator], None]] = None,
    restore: Optional[Callable[[], None]] = None,
    n_boot: int = 200,
    min_pct: float = 0.05,
    seed: int = 0,
    equivalent_jaccard: float = 0.95,
) -> Attribution:
    """Clean the candidate set, then compute Owen values with uncertainty.

    ``resample(rng)`` mutates the game into one bootstrap replicate; ``restore()`` undoes it.
    For SketchGame, uncertainty comes from the W baseline weeks instead.
    """
    rng = np.random.default_rng(seed)
    log: list[str] = []
    K = game.K
    E = len(game.effects)
    vol = game.volume()
    active = vol > 0
    flags = game.flags

    # 1. Equivalent supports merge
    groups: list[list[int]] = []
    for k in range(K):
        for g in groups:
            a, b = flags[active, g[0]], flags[active, k]
            inter, union = vol[active][a & b].sum(), vol[active][a | b].sum()
            if union > 0 and inter / union >= equivalent_jaccard:
                g.append(k)
                log.append(f"equivalent: {labels[k]} = {labels[g[0]]}")
                break
        else:
            groups.append([k])
    cands = [Candidate(labels[g[0]], [labels[j] for j in g], g[0]) for g in groups]
    for c, g in zip(cands, groups):
        c.share_of_volume = float(vol[flags[:, g].any(axis=1)].sum() / vol.sum())
    # Equivalent columns act as one: give the representative the union of their cells.
    for g in groups:
        flags[:, g[0]] = flags[:, g].any(axis=1)

    def players_of(cs: Sequence[Candidate]) -> list[int]:
        return [game.player(c.column, e) for c in cs for e in game.effects]

    def v(cs: Sequence[Candidate]) -> float:
        return float(game.value(_coalition(players_of(cs), game.n_players))[0]) if cs else 0.0

    anomaly = game.anomaly()

    def draws(rows: np.ndarray, n: int) -> np.ndarray:
        """(R x B) values of the coalitions in ``rows`` under resampling: bootstrap replicates
        (additive) or one row per baseline week (count-distinct)."""
        if isinstance(game, SketchGame):
            return game.value_by_week(rows)
        if resample is None:
            return game.value(rows)[None, :]
        out = []
        for _ in range(n):
            resample(rng)
            out.append(game.value(rows))
        restore()
        return np.array(out)

    # Every candidate is a hypothesis test: Bonferroni over the initial candidate count, so a
    # pool of 12 does not let one noise segment through at the 95% level.
    z_crit = float(stats.norm.ppf(1 - 0.025 / max(len(cands), 1)))

    def interval(samples: np.ndarray, point: np.ndarray, z: float = z_crit) -> tuple[np.ndarray, np.ndarray]:
        if len(samples) <= 1:
            return point, point
        if isinstance(game, SketchGame):  # draws are the baseline weeks: SE of their mean
            se = samples.std(axis=0, ddof=1) / math.sqrt(len(samples))
        else:  # bootstrap replicates: their spread is the SE
            se = samples.std(axis=0, ddof=1)
        return point - z * se, point + z * se

    def rows_for(groups_of_cands: list[list[Candidate]]) -> np.ndarray:
        return np.vstack([_coalition(players_of(cs), game.n_players) for cs in groups_of_cands])

    n_quick = max(50, n_boot // 4)

    # 2. Nesting by effect homogeneity. The rest of B (B minus A) decides:
    #    not significantly different from 0  -> B is a loose superset: keep the specific A
    #    same per-unit effect as A (rho > .67) -> A is a slice of a homogeneous B: keep B
    #    The significance test matters: harvested sub-cells suffer from the winner's curse
    #    (they were selected *because* noise inflated them), which biases rho toward 0.
    alive = list(cands)

    def cells(c: Candidate) -> np.ndarray:
        return flags[:, c.column] & active

    def nested(x: Candidate, y: Candidate) -> bool:
        fx, fy = cells(x), cells(y)
        return bool(fx.any() and fy.any() and (not (fx & ~fy).any() or not (fy & ~fx).any()))

    for a in list(alive):
        for b in list(alive):
            if a is b or a not in alive or b not in alive:
                continue
            fa, fb = cells(a), cells(b)
            if not fa.any() or (fa & ~fb).any() or not (fb & ~fa).any():
                continue  # a is not strictly inside b
            # Condition on the other causes: on a level metric, halving email lowers orders in
            # every segment, so "the rest of B" looks affected unless email is reverted first.
            # Candidates nested with a or b are left out of the context (they would cover them).
            ctx = [c for c in alive if c is not a and c is not b and not nested(c, a) and not nested(c, b)]
            covered = np.zeros(len(flags), bool)
            for c in ctx:
                covered |= cells(c)
            if not (fb & ~fa & ~covered).any():  # context hides all of B minus A: compare plainly
                ctx, covered = [], np.zeros(len(flags), bool)
            rows = rows_for([ctx, ctx + [a], ctx + [b]])
            point = game.value(rows)
            samples = draws(rows, n_quick)
            vol_a = vol[fa & ~covered].sum()
            vol_rest = vol[fb & ~fa & ~covered].sum()
            eff_a = point[1] - point[0]
            if eff_a == 0 or vol_rest == 0 or vol_a == 0:
                continue

            def rho_of(v):  # per-unit effect of B minus A, relative to A's
                with np.errstate(divide="ignore", invalid="ignore"):
                    return ((v[..., 2] - v[..., 1]) / vol_rest) / ((v[..., 1] - v[..., 0]) / vol_a)

            rho = float(rho_of(point))
            r_samples = rho_of(samples)
            r_samples = r_samples[np.isfinite(r_samples)]
            lo, hi = (interval(r_samples[:, None], np.array([rho]), z=1.96) if len(r_samples) > 1
                      else ([rho - 1e-6], [rho + 1e-6]))
            has_one, has_zero = lo[0] <= 1 <= hi[0], lo[0] <= 0 <= hi[0]
            # Decide on the interval, not the point: a homogeneous effect's slices have rho ~ 1
            # but a noisy point estimate, and a hard cut-off would flip the answer run to run.
            # When the data cannot tell the two apart, prefer the general segment B: it has more
            # data and makes the weaker claim. A is preferred only when the rest of B demonstrably
            # did not move the same way (the interval excludes 1).
            if has_one:
                verdict = "slice"
            elif has_zero:
                verdict = "superset"
            else:
                verdict = "both"
            if verdict == "slice":
                alive.remove(a)
                a.status, a.reason = "rejected", (f"slice of {b.label}: the rest of it moves the same way "
                                                  f"({rho:.0%} of the per-unit effect)")
            elif verdict == "superset":
                alive.remove(b)
                b.status, b.reason = "rejected", (f"loose superset of {a.label}: the rest of it has no "
                                                  f"significant effect ({rho:.0%} of the per-unit effect)")
            rest_zero = verdict == "superset"
            log.append(f"nesting {a.label} in {b.label}: rho={rho:.2f} (CI {lo[0]:.2f}..{hi[0]:.2f}) -> {verdict}")

    # Nested pairs that both survived describe different effects (a concentrated core inside a
    # broader, weaker one). Make them disjoint so leave-one-out is meaningful for both:
    # otherwise the inner one has zero unique contribution by construction.
    for a in alive:
        for b in alive:
            fa, fb = cells(a), cells(b)
            if a is not b and fa.any() and not (fa & ~fb).any() and (fb & ~fa).any():
                flags[:, b.column] &= ~flags[:, a.column]
                b.label = f"{b.label} (excluding {a.label})"
                log.append(f"disjoint: {b.label}")

    # 3. Backward elimination by leave-one-out contribution
    def loo_rows(cs: list[Candidate]) -> np.ndarray:
        return rows_for([cs] + [[c for c in cs if c is not x] for x in cs])

    while alive:
        rows = loo_rows(alive)
        point = game.value(rows)
        loo = point[0] - point[1:]
        samples = draws(rows, n_quick)
        lo, hi = interval(samples[:, :1] - samples[:, 1:], loo)
        weak = [(abs(l) / abs(anomaly) if anomaly else 0.0, i) for i, l in enumerate(loo)
                if abs(l) < min_pct * abs(anomaly) or (lo[i] <= 0 <= hi[i])]
        for i, c in enumerate(alive):
            c.loo = float(loo[i])
        if not weak:
            break
        _, i = min(weak)
        c = alive.pop(i)
        c.status = "rejected"
        lo_p, hi_p = sorted((lo[i] / anomaly, hi[i] / anomaly)) if anomaly else (0, 0)
        c.reason = (f"no unique contribution given the others (LOO {loo[i] / anomaly:+.0%} of anomaly, "
                    f"CI {lo_p:+.0%}..{hi_p:+.0%})" if anomaly else "no contribution")
        log.append(f"eliminated {c.label}: {c.reason}")

    # 4. Owen values over survivors
    if alive:
        unions = [[game.player(c.column, e) for e in game.effects] for c in alive]
        sub = [p for u in unions for p in u]
        remap = {p: i for i, p in enumerate(sub)}
        local_unions = [[remap[p] for p in u] for u in unions]
        C_local, terms = owen_terms(local_unions, len(sub))
        C = np.zeros((len(C_local), game.n_players), bool)
        C[:, sub] = C_local

        if isinstance(game, SketchGame):
            Vw = game.value_by_week(C)
            phi_w = owen_from_values(Vw, terms, len(sub))
            phi = phi_w.mean(axis=0)
            se = phi_w.std(axis=0, ddof=1) / math.sqrt(len(Vw)) if len(Vw) > 1 else np.zeros_like(phi)
            boot_phi = None
        else:
            phi = owen_from_values(game.value(C), terms, len(sub))
            boot_phi = None
            if resample is not None:
                reps = []
                for _ in range(n_boot):
                    resample(rng)
                    reps.append(owen_from_values(game.value(C), terms, len(sub)))
                restore()
                boot_phi = np.array(reps)
        for j, c in enumerate(alive):
            idxs = [remap[p] for p in unions[j]]
            c.phi = float(phi[idxs].sum())
            c.phi_by_effect = {e: float(phi[i]) for e, i in zip(game.effects, idxs)} if E > 1 else {}
            if boot_phi is not None:
                c.ci = tuple(np.percentile(boot_phi[:, idxs].sum(axis=1), [2.5, 97.5]))
            elif isinstance(game, SketchGame):
                s = float(np.sqrt((se[idxs] ** 2).sum()))
                c.ci = (c.phi - 2 * s, c.phi + 2 * s)
        explained = v(alive)
    else:
        explained = 0.0

    residual = anomaly - explained
    return Attribution(cands, game.actual(), game.expected(), anomaly, explained, residual,
                       (float("nan"), float("nan")), log)
