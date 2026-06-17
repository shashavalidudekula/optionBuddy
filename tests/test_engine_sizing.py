"""Pure-logic tests for the advisory engine: call validation + ATR sizing."""
import pytest

from signals import advisory_engine as ae


# ── _is_valid: required fields + coherent price ordering ──────────────────────

def test_valid_buy_call():
    assert ae._is_valid({
        "instrument": "NIFTY 24500 CE", "action": "BUY",
        "entry_price": 100, "target_1": 120, "stop_loss": 90,
    })


def test_valid_sell_call():
    assert ae._is_valid({
        "instrument": "NIFTY 24500 PE", "action": "SELL",
        "entry_price": 100, "target_1": 80, "stop_loss": 110,
    })


def test_reject_buy_with_target_below_entry():
    assert not ae._is_valid({
        "instrument": "x", "action": "BUY",
        "entry_price": 100, "target_1": 90, "stop_loss": 95,
    })


def test_reject_sell_with_target_above_entry():
    assert not ae._is_valid({
        "instrument": "x", "action": "SELL",
        "entry_price": 100, "target_1": 120, "stop_loss": 110,
    })


def test_reject_missing_required_field():
    assert not ae._is_valid({"action": "BUY", "entry_price": 100, "target_1": 120})


# ── _clamp: distances stay within a sane % of premium ────────────────────────

def test_clamp_floor_and_ceiling():
    assert ae._clamp(5, 100, (0.12, 0.35)) == 12   # below floor → floored
    assert ae._clamp(50, 100, (0.12, 0.35)) == 35  # above ceiling → capped
    assert ae._clamp(20, 100, (0.12, 0.35)) == 20  # in range → unchanged


# ── _delta_for: resolve the chosen strike's delta from the chain ─────────────

def test_delta_for_resolves_strike():
    call = {"underlying": "NIFTY", "instrument": "NIFTY 24500 CE"}
    market = {"option_chain": {"NIFTY": {"strikes": [
        {"strike": 24500, "option_type": "CE", "delta": 0.42},
        {"strike": 24500, "option_type": "PE", "delta": -0.58},
    ]}}}
    assert ae._delta_for(call, market) == pytest.approx(0.42)


def test_delta_for_missing_chain_returns_none():
    assert ae._delta_for({"underlying": "NIFTY", "instrument": "NIFTY 24500 CE"}, {}) is None


# ── _size_by_atr: volatility-scaled premium levels, clamped & ordered ─────────

def test_size_by_atr_buy_orders_and_clamps(monkeypatch):
    monkeypatch.setattr(ae, "ATR_SIZING_ENABLED", True)
    call = {
        "category": "index_option", "underlying": "NIFTY",
        "instrument": "NIFTY 24500 CE", "action": "BUY", "entry_price": 100.0,
    }
    market = {
        "intraday": {"NIFTY": {"atr14_5m": 10.0}},
        "option_chain": {"NIFTY": {"strikes": [
            {"strike": 24500, "option_type": "CE", "delta": 0.5},
        ]}},
    }
    ae._size_by_atr(call, market)

    assert call["target_2"] > call["target_1"] > call["entry_price"] > call["stop_loss"]
    # SL distance clamped to the engine's sane band (12%–35% of premium).
    sl_dist = call["entry_price"] - call["stop_loss"]
    assert 100.0 * ae._ATR_SL_PCT[0] <= sl_dist <= 100.0 * ae._ATR_SL_PCT[1]


def test_size_by_atr_noop_without_atr(monkeypatch):
    monkeypatch.setattr(ae, "ATR_SIZING_ENABLED", True)
    call = {
        "category": "index_option", "underlying": "NIFTY",
        "instrument": "NIFTY 24500 CE", "action": "BUY", "entry_price": 100.0,
        "target_1": 130.0, "stop_loss": 80.0,
    }
    ae._size_by_atr(call, {"intraday": {}})  # no atr14_5m → leave model levels alone
    assert call["target_1"] == 130.0 and call["stop_loss"] == 80.0
