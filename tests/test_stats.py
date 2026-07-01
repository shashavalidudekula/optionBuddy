"""Tests for pairs/mean-reversion statistics (core/quant/stats.py)."""
import random
import pytest

from core.quant import stats as st


def _cointegrated(n=400, beta=1.5, ar=0.9, seed=1):
    """B = random walk; A = beta*B + mean-reverting(AR1) spread → A,B cointegrated."""
    random.seed(seed)
    b = [100.0]
    spread = [0.0]
    for _ in range(1, n):
        b.append(b[-1] * (1 + random.gauss(0, 0.01)))
        spread.append(ar * spread[-1] + random.gauss(0, 1.0))
    a = [beta * b[i] + spread[i] for i in range(n)]
    return a, b


def test_ols_beta_recovers_hedge_ratio():
    a, b = _cointegrated(beta=1.5)
    assert st.ols_beta(a, b) == pytest.approx(1.5, abs=0.25)


def test_correlation_high_for_cointegrated():
    a, b = _cointegrated(beta=1.5)
    assert st.correlation(a, b) > 0.9


def test_half_life_positive_for_mean_reverting():
    # AR(1) coeff 0.9 → λ≈−0.1 → half-life ≈ ln2/0.1 ≈ 6.9
    a, b = _cointegrated(beta=1.5, ar=0.9)
    sig = st.pair_signal(a, b)
    assert sig["half_life"] is not None
    assert 2 < sig["half_life"] < 25


def test_zscore_of_stretched_spread():
    series = [0, 0, 0, 0, 5.0]   # last point far above the mean
    z, m, sd = st.zscore(series)
    assert z > 1.5


def test_half_life_none_when_too_short():
    assert st.half_life([1.0, 2.0]) is None


def test_pair_signal_shape():
    a, b = _cointegrated()
    sig = st.pair_signal(a, b)
    assert set(sig) == {"beta", "z", "spread_mean", "spread_std", "half_life", "corr"}
