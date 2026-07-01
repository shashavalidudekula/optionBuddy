"""Pure-logic tests for the call tracker: stop-trailing, return %, entry zone."""
import pytest

from core import call_tracker as ct


@pytest.fixture(autouse=True)
def fixed_trail(monkeypatch):
    # Pin the trail fraction so assertions don't depend on the local .env.
    monkeypatch.setattr(ct, "T1_TRAIL_LOCK_FRACTION", 0.75)


# ── t1_locked_stop: lock most of the entry→T1 move, keep some room ────────────

def test_t1_locked_stop_buy():
    # entry 100, T1 120, lock 0.75 → stop sits at 115 (25% of the move below T1)
    assert ct.t1_locked_stop("BUY", 100.0, 120.0) == pytest.approx(115.0)


def test_t1_locked_stop_sell():
    # SELL mirrors: entry 100, T1 80 → 100 - (100-80)*0.75 = 85
    assert ct.t1_locked_stop("SELL", 100.0, 80.0) == pytest.approx(85.0)


def test_t1_locked_stop_sits_between_entry_and_t1_for_buy():
    s = ct.t1_locked_stop("BUY", 100.0, 120.0)
    assert 100.0 < s < 120.0  # profit locked, but not a stop exactly at T1


def test_t1_locked_stop_breakeven_when_fraction_zero(monkeypatch):
    monkeypatch.setattr(ct, "T1_TRAIL_LOCK_FRACTION", 0.0)
    assert ct.t1_locked_stop("BUY", 100.0, 120.0) == pytest.approx(100.0)


# ── return % ─────────────────────────────────────────────────────────────────

def test_pct_buy_and_sell():
    assert ct._pct("BUY", 100.0, 110.0) == pytest.approx(10.0)
    assert ct._pct("SELL", 100.0, 90.0) == pytest.approx(10.0)


def test_pct_guards_zero_entry():
    assert ct._pct("BUY", 0.0, 110.0) == 0.0


# ── entry zone: fill on dips AND momentum, reject only the wrong side ─────────

def test_buy_fills_at_or_below_max():
    call = {"entry_price": 100, "entry_min": 98, "entry_max": 102}
    assert ct._in_entry_zone("BUY", 101, call) is True   # inside the band
    assert ct._in_entry_zone("BUY", 95, call) is True    # a dip below max still fills
    assert ct._in_entry_zone("BUY", 103, call) is False  # above max — too expensive


def test_sell_fills_at_or_above_min():
    call = {"entry_price": 100, "entry_min": 98, "entry_max": 102}
    assert ct._in_entry_zone("SELL", 99, call) is True
    assert ct._in_entry_zone("SELL", 97, call) is False


def test_entry_zone_falls_back_to_entry_price():
    call = {"entry_price": 100}  # no band provided
    assert ct._in_entry_zone("BUY", 100, call) is True
    assert ct._in_entry_zone("BUY", 101, call) is False
