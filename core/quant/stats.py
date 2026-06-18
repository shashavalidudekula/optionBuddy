"""
stats.py — pairs / mean-reversion statistics (pure stdlib).

Primitives for stat-arb: the OLS hedge ratio between two price series, the spread
they form, its z-score (how stretched it is now), its half-life of mean reversion
(from an AR(1)/Ornstein-Uhlenbeck fit), and their correlation. A pair is tradable
when it's well correlated AND the spread mean-reverts on a usable horizon; we then
trade the z-score back to the mean. No numpy — clear, testable closed forms.
"""
import math


def mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def ols_beta(y: list, x: list) -> float:
    """Slope of y on x (hedge ratio): cov(x,y) / var(x)."""
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    mx, my = mean(x[:n]), mean(y[:n])
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    var = sum((x[i] - mx) ** 2 for i in range(n))
    return cov / var if var else 0.0


def correlation(a: list, b: list) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = mean(a[:n]), mean(b[:n])
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((a[i] - ma) ** 2 for i in range(n))
    vb = sum((b[i] - mb) ** 2 for i in range(n))
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def spread_series(a: list, b: list, beta: float) -> list:
    """Spread = a − β·b (the residual that should be stationary if cointegrated)."""
    n = min(len(a), len(b))
    return [a[i] - beta * b[i] for i in range(n)]


def zscore(series: list) -> tuple:
    """(z, mean, std) of the LAST point vs the series. z is how many sigmas the
    current spread sits from its mean."""
    if len(series) < 2:
        return 0.0, (series[-1] if series else 0.0), 0.0
    m = mean(series)
    sd = math.sqrt(sum((x - m) ** 2 for x in series) / (len(series) - 1))
    z = (series[-1] - m) / sd if sd > 0 else 0.0
    return z, m, sd


def half_life(series: list) -> float | None:
    """Mean-reversion half-life via AR(1): Δs_t = a + λ·s_{t-1} + ε.
    half-life = −ln2/λ (needs λ<0). None if the spread isn't mean-reverting."""
    if len(series) < 3:
        return None
    level = series[:-1]
    delta = [series[i] - series[i - 1] for i in range(1, len(series))]
    lam = ols_beta(delta, level)
    if lam >= 0:
        return None
    return -math.log(2.0) / lam


def pair_signal(window_a: list, window_b: list) -> dict:
    """Summarise a pair over a lookback window: hedge ratio, current z, half-life,
    correlation. The strategy decides tradability + direction from these."""
    beta = ols_beta(window_a, window_b)
    spread = spread_series(window_a, window_b, beta)
    z, m, sd = zscore(spread)
    return {"beta": beta, "z": z, "spread_mean": m, "spread_std": sd,
            "half_life": half_life(spread), "corr": correlation(window_a, window_b)}
