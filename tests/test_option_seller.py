"""Tests for deterministic option-selling call generation."""
import pytest

from core import option_seller as seller


# ── strike selection by delta ────────────────────────────────────────────────

def _make_chain(opt_type, strikes_deltas_ivs):
    """Build a mock chain {strikes: [{strike, delta, iv, premium, option_type}]}."""
    strikes = []
    for strike, delta, iv in strikes_deltas_ivs:
        strikes.append({
            "strike": strike, "delta": delta, "iv": iv, "premium": abs(delta) * 10,
            "option_type": opt_type,
        })
    return {"strikes": strikes}


def test_pick_strike_by_delta_finds_closest():
    # Chain with strikes at various deltas; pick_strike_by_delta should find closest to 0.25.
    chain = _make_chain("CE", [(20000, 0.10, 0.20), (20100, 0.25, 0.22), (20200, 0.35, 0.21)])
    pick = seller.pick_strike_by_delta(chain, "CE", 20100.0, target_delta=0.25)
    assert pick is not None
    assert float(pick["strike"]) == 20100
    assert float(pick["delta"]) == 0.25


def test_pick_strike_by_delta_none_when_no_legs():
    chain = _make_chain("CE", [])
    pick = seller.pick_strike_by_delta(chain, "CE", 20100.0, target_delta=0.25)
    assert pick is None


def test_pick_strike_by_delta_prefers_higher_iv_when_tied():
    # Two strikes at the same delta; prefer higher IV.
    chain = _make_chain("PE", [(19900, 0.25, 0.18), (19800, 0.25, 0.25)])
    pick = seller.pick_strike_by_delta(chain, "PE", 19850.0, target_delta=0.25, prefer_iv=True)
    assert pick is not None
    assert float(pick["strike"]) == 19800  # Higher IV


# ── protective wing selection ────────────────────────────────────────────────

def test_pick_wing_strike_out_for_call():
    # For a short call at 20100, wing is 20100 + 100 = 20200 (further OTM).
    chain = _make_chain("CE", [(20100, 0.25, 0.20), (20200, 0.10, 0.18), (20300, 0.05, 0.17)])
    wing = seller.pick_wing_strike(chain, 20100, "CE", 100, direction="out")
    assert wing is not None
    assert int(wing["strike"]) == 20200


def test_pick_wing_strike_out_for_put():
    # For a short put at 19900, wing is 19900 - 100 = 19800 (further OTM, lower).
    chain = _make_chain("PE", [(19900, 0.25, 0.20), (19800, 0.10, 0.18), (19700, 0.05, 0.17)])
    wing = seller.pick_wing_strike(chain, 19900, "PE", 100, direction="out")
    assert wing is not None
    assert int(wing["strike"]) == 19800


def test_pick_wing_strike_none_when_missing():
    chain = _make_chain("CE", [(20100, 0.25, 0.20)])  # No 20200 strike
    wing = seller.pick_wing_strike(chain, 20100, "CE", 100, direction="out")
    assert wing is None


# ── sanity on expected configs ───────────────────────────────────────────────

def test_config_loaded():
    from config.settings import (
        SELLING_ENABLED, SELLING_DELTA_TARGET, SELLING_SPREAD_WIDTH,
        SELLING_STRUCTURE, SELLING_HOLD_DAYS,
    )
    # Default values (may be overridden in .env).
    assert SELLING_DELTA_TARGET > 0
    assert SELLING_SPREAD_WIDTH > 0
    assert SELLING_HOLD_DAYS > 0
    assert SELLING_STRUCTURE in ("spread", "naked")
