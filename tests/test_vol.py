"""Tests for volatility analytics (core/quant/vol.py)."""
import math
import pytest

from core.quant import vol as v


def test_log_returns_skips_bad_points():
    assert v.log_returns([100, 110]) == pytest.approx([math.log(1.1)])
    # zero/negative prices are skipped (no div-by-zero, no log of <=0)
    assert v.log_returns([100, 0, 110]) == []


def test_realized_vol_zero_for_flat_series():
    assert v.realized_vol([100, 100, 100, 100]) == pytest.approx(0.0)


def test_realized_vol_positive_and_annualised():
    # alternating +1%/-1% returns → non-zero annualised vol
    closes = [100]
    for i in range(20):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    rv = v.realized_vol(closes)
    assert rv is not None and rv > 0


def test_realized_vol_none_when_too_short():
    assert v.realized_vol([100]) is None


def test_parkinson_positive():
    highs = [101, 102, 103]
    lows = [99, 100, 101]
    pv = v.parkinson_vol(highs, lows)
    assert pv is not None and pv > 0


# ── IV rank / percentile (the "is premium rich?" signal) ─────────────────────

def test_iv_rank_midpoint():
    assert v.iv_rank(20, [10, 30]) == pytest.approx(0.5)


def test_iv_rank_clamped():
    assert v.iv_rank(40, [10, 30]) == pytest.approx(1.0)   # above max → clamp 1
    assert v.iv_rank(5, [10, 30]) == pytest.approx(0.0)    # below min → clamp 0


def test_iv_rank_flat_history():
    assert v.iv_rank(15, [15, 15, 15]) == pytest.approx(0.5)


def test_iv_percentile():
    assert v.iv_percentile(20, [10, 15, 20, 25]) == pytest.approx(0.5)  # 10,15 below
    assert v.iv_percentile(100, [10, 15, 20]) == pytest.approx(1.0)


def test_vrp_sign():
    assert v.vrp(0.16, 0.12) == pytest.approx(0.04)   # implied richer → sell is paid
    assert v.vrp(0.10, 0.15) == pytest.approx(-0.05)  # implied cheap → don't sell
