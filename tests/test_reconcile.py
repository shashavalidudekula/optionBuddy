"""Tests for the pure position-reconciliation logic."""
from core import reconcile as rc


# ── reconcile_positions: compare two {code: net_qty} maps ────────────────────

def test_in_sync_no_divergence():
    internal = {"NSE_FNO:111": 50, "NSE_FNO:222": -25}
    broker = {"NSE_FNO:111": 50, "NSE_FNO:222": -25}
    assert rc.reconcile_positions(internal, broker) == []


def test_qty_mismatch_flagged_with_signed_diff():
    internal = {"NSE_FNO:111": 50}
    broker = {"NSE_FNO:111": 25}  # only half filled
    out = rc.reconcile_positions(internal, broker)
    assert out == [{"code": "NSE_FNO:111", "internal": 50, "broker": 25, "diff": -25}]


def test_position_missing_at_broker():
    # Bot thinks it holds it; broker has nothing (rejected entry).
    out = rc.reconcile_positions({"NSE_FNO:111": 50}, {})
    assert out == [{"code": "NSE_FNO:111", "internal": 50, "broker": 0, "diff": -50}]


def test_unexpected_broker_position():
    # Broker holds something the bot doesn't know about.
    out = rc.reconcile_positions({}, {"NSE_FNO:999": 35})
    assert out == [{"code": "NSE_FNO:999", "internal": 0, "broker": 35, "diff": 35}]


def test_tolerance_suppresses_small_diff():
    out = rc.reconcile_positions({"X:1": 50}, {"X:1": 49}, qty_tol=1)
    assert out == []


# ── net_by_code: collapse internal positions into signed net per code ─────────

def _resolver(p):
    return {"BANKNIFTY": "NSE_FNO:25", "NIFTY": "NSE_FNO:13"}.get(p.get("underlying"))


def test_net_by_code_signs_and_sums():
    positions = [
        {"underlying": "NIFTY", "action": "BUY", "remaining_qty": 75},
        {"underlying": "NIFTY", "action": "BUY", "remaining_qty": 75},
        {"underlying": "BANKNIFTY", "action": "SELL", "remaining_qty": 30},
    ]
    assert rc.net_by_code(positions, _resolver) == {"NSE_FNO:13": 150, "NSE_FNO:25": -30}


def test_net_by_code_falls_back_to_lots_times_lotsize():
    positions = [{"underlying": "NIFTY", "action": "BUY", "lots": 2, "lot_size": 75}]
    assert rc.net_by_code(positions, _resolver) == {"NSE_FNO:13": 150}


def test_net_by_code_skips_unresolvable():
    positions = [{"underlying": "WHO_DIS", "action": "BUY", "remaining_qty": 10}]
    assert rc.net_by_code(positions, _resolver) == {}
