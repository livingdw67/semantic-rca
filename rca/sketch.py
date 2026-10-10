"""HyperLogLog registers: decode, merge, estimate.

Registers are built in SQL by the planner (``SlicePlanner.flag_sketches``): for each element,
h = hash64(key), register = h mod 2^p, rho = leading zeros of (h >> p) + 1, and each register
keeps the max rho. That relational form is portable (any warehouse with a 64-bit hash) and,
crucially, *mergeable*: the sketch of a union of cells is the element-wise max of their sketches.
That is what lets the attribution stage evaluate any counterfactual union of cells without
another query.

Estimator: the original HLL estimate with linear counting for the small range (Flajolet et al.
2007). With p=14 (16,384 registers, ~0.8% standard error) linear counting covers sets up to
~40k elements, which is where HLL's raw estimate is most biased; beyond ~80k the raw estimate is
nearly unbiased. HLL++'s empirical bias tables are not implemented, so sets of ~40k-80k carry a
small (~1%) bias -- the same direction for actual and counterfactual, which mostly cancels.
"""

from __future__ import annotations

import math

import numpy as np


def decode(encoded: str | None, precision: int) -> np.ndarray:
    """'reg:rho,reg:rho,...' -> dense uint8 register array of length 2^p."""
    regs = np.zeros(1 << precision, dtype=np.uint8)
    if not encoded:
        return regs
    flat = np.array(encoded.replace(":", ",").split(","), dtype=np.int64).reshape(-1, 2)
    np.maximum.at(regs, flat[:, 0], flat[:, 1].astype(np.uint8))
    return regs


def merge(registers: np.ndarray) -> np.ndarray:
    """Union of sketches: element-wise max over the leading axis."""
    if registers.ndim == 1:
        return registers
    if len(registers) == 0:
        return np.zeros(registers.shape[-1], dtype=np.uint8)
    return registers.max(axis=0)


def estimate(registers: np.ndarray, precision: int) -> float:
    m = 1 << precision
    regs = merge(registers) if registers.ndim > 1 else registers
    alpha = 0.7213 / (1 + 1.079 / m)
    raw = alpha * m * m / float(np.sum(np.ldexp(1.0, -regs.astype(np.int32))))
    zeros = int(np.count_nonzero(regs == 0))
    if raw <= 2.5 * m and zeros > 0:
        return m * math.log(m / zeros)
    return raw


def registers_of(hashes: np.ndarray, precision: int) -> np.ndarray:
    """Python reference implementation (for tests): uint64 hashes -> registers."""
    h = hashes.astype(np.uint64)
    reg = (h & np.uint64((1 << precision) - 1)).astype(np.int64)
    tail = (h >> np.uint64(precision)).astype(np.float64)
    bits = 64 - precision
    rho = np.where(tail == 0, bits + 1, bits - np.floor(np.log2(np.maximum(tail, 1))).astype(np.int64))
    regs = np.zeros(1 << precision, dtype=np.uint8)
    np.maximum.at(regs, reg, rho.astype(np.uint8))
    return regs
