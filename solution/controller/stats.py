"""Small, deterministic statistics helpers.

No randomness and no LLM is used for winner selection: given the same counts
the same arm is always declared the winner.  Everything is pure stdlib.
"""
from __future__ import annotations

import math

Z_95 = 1.959963984540054


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def z_two_proportion(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """Return (z, one-sided p) for H1: p1 > p2.

    Uses the pooled proportion and a continuity-free normal approximation.
    """
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    p1, p2 = s1 / n1, s2 / n2
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(max(p * (1.0 - p) * (1.0 / n1 + 1.0 / n2), 1e-18))
    z = (p1 - p2) / se
    return z, 1.0 - norm_cdf(z)


def z_two_sided(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    z, p1 = z_two_proportion(s1, n1, s2, n2)
    # p1 is one-sided upper tail; two-sided is 2*min(tail)
    p = min(1.0, 2.0 * min(p1, 1.0 - p1))
    return z, p


def wilson(s: int, n: int, z: float = Z_95) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = s / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def shrink_rate(s: int, n: int, prior: float, k: float = 20.0) -> float:
    """Empirical-Bayes style shrinkage toward `prior` with pseudo-count k."""
    return (s + prior * k) / (n + k)


def holm(pvalues: list[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down.  Returns rejection flags aligned to input."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    for rank, i in enumerate(order):
        if pvalues[i] <= alpha / (m - rank):
            reject[i] = True
        else:
            break
    return reject


def bh(pvalues: list[float], alpha: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg FDR control."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff = -1
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= alpha * rank / m:
            cutoff = rank
    reject = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= cutoff:
            reject[i] = True
    return reject


def required_n(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Approximate per-arm sample size for a two-proportion test."""
    za = norm_icdf(1.0 - alpha / 2.0)
    zb = norm_icdf(power)
    pbar = (p1 + p2) / 2.0
    delta = abs(p1 - p2)
    if delta <= 1e-9:
        return 10**9
    return int(math.ceil(2.0 * (za + zb) ** 2 * pbar * (1 - pbar) / delta ** 2))


def norm_icdf(p: float) -> float:
    """Acklam's rational approximation, accurate enough for planning."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
