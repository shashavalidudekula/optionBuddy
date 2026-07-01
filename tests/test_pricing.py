"""Tests for the Black-Scholes pricing/greeks/IV-solver (core/quant/pricing.py)."""
import math
import pytest

from core.quant import pricing as p


# ── known closed-form values ─────────────────────────────────────────────────

def test_atm_call_known_value():
    # S=K=100, T=1, r=0, sigma=0.20 → BS call ≈ 7.9656
    val = p.bs_price(100, 100, 1.0, 0.0, 0.20, "CE")
    assert val == pytest.approx(7.9656, abs=1e-3)


def test_put_call_parity_at_zero_rate():
    # r=q=0 → call and put are equal at the money
    c = p.bs_price(100, 100, 1.0, 0.0, 0.20, "CE")
    pu = p.bs_price(100, 100, 1.0, 0.0, 0.20, "PE")
    assert c == pytest.approx(pu, abs=1e-6)


def test_put_call_parity_general():
    # C - P == S*e^{-qT} - K*e^{-rT}
    S, K, T, r, q, sig = 24000, 24200, 30 / 365, 0.065, 0.0, 0.14
    c = p.bs_price(S, K, T, r, sig, "CE", q)
    pu = p.bs_price(S, K, T, r, sig, "PE", q)
    lhs = c - pu
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert lhs == pytest.approx(rhs, abs=1e-2)


# ── intrinsic fallbacks (T<=0, sigma<=0) ─────────────────────────────────────

def test_expiry_settles_to_intrinsic():
    assert p.bs_price(120, 100, 0.0, 0.06, 0.2, "CE") == pytest.approx(20.0)
    assert p.bs_price(120, 100, 0.0, 0.06, 0.2, "PE") == pytest.approx(0.0)
    assert p.bs_price(90, 100, -1, 0.06, 0.2, "PE") == pytest.approx(10.0)


def test_zero_vol_is_intrinsic():
    assert p.bs_price(110, 100, 0.5, 0.0, 0.0, "CE") == pytest.approx(10.0)


# ── greeks ───────────────────────────────────────────────────────────────────

def test_atm_call_delta_near_half():
    g = p.bs_greeks(100, 100, 1.0, 0.0, 0.20, "CE")
    assert g["delta"] == pytest.approx(0.5398, abs=1e-3)   # Phi(0.1)


def test_put_delta_is_negative_and_call_positive():
    cg = p.bs_greeks(24000, 24500, 30 / 365, 0.065, 0.14, "CE")
    pg = p.bs_greeks(24000, 23500, 30 / 365, 0.065, 0.14, "PE")
    assert 0 < cg["delta"] < 1
    assert -1 < pg["delta"] < 0
    assert cg["vega"] > 0 and cg["gamma"] > 0


# ── implied-vol round trip ───────────────────────────────────────────────────

@pytest.mark.parametrize("sigma", [0.08, 0.14, 0.25, 0.40])
@pytest.mark.parametrize("opt", ["CE", "PE"])
def test_iv_roundtrip(sigma, opt):
    S, K, T, r = 24000, 24200, 45 / 365, 0.065
    price = p.bs_price(S, K, T, r, sigma, opt)
    iv = p.implied_vol(price, S, K, T, r, opt)
    assert iv == pytest.approx(sigma, abs=1e-3)


def test_iv_none_when_price_below_intrinsic():
    # price below discounted intrinsic can't be matched by any vol
    assert p.implied_vol(0.01, 120, 100, 1.0, 0.06, "CE") is None
