"""Money-math + fill-realism regression net for the paper trader.

The headline behaviour locked in here is the #1 fix: a stop is a MARKET exit, so a
long position must NEVER be booked better than its stop trigger, and slippage is
always adverse. These are the numbers the whole go-live decision rests on.
"""
import pytest

from core import paper_trader as pt

SLIP = 0.02  # deterministic 2%/leg for assertions, independent of the local .env


@pytest.fixture(autouse=True)
def fixed_slip(monkeypatch):
    monkeypatch.setattr(pt, "PAPER_SLIPPAGE_PCT", SLIP)


# ── slippage direction & magnitude ──────────────────────────────────────────

def test_slip_buy_pays_up():
    assert pt._slip_fill(100.0, "buy") == pytest.approx(102.0)


def test_slip_sell_receives_less():
    assert pt._slip_fill(100.0, "sell") == pytest.approx(98.0)


@pytest.mark.parametrize("price", [1.0, 12.5, 250.0, 1000.0])
def test_slip_is_always_adverse(price):
    assert pt._slip_fill(price, "buy") >= price     # never a cheaper buy
    assert pt._slip_fill(price, "sell") <= price    # never a richer sell


def test_slip_zero_disables(monkeypatch):
    monkeypatch.setattr(pt, "PAPER_SLIPPAGE_PCT", 0.0)
    assert pt._slip_fill(100.0, "buy") == 100.0
    assert pt._slip_fill(100.0, "sell") == 100.0


def test_slip_negative_is_clamped_not_favourable(monkeypatch):
    # A misconfigured negative slippage must not flip into a FAVOURABLE fill.
    monkeypatch.setattr(pt, "PAPER_SLIPPAGE_PCT", -0.05)
    assert pt._slip_fill(100.0, "buy") == 100.0
    assert pt._slip_fill(100.0, "sell") == 100.0


# ── cash / P&L accounting ────────────────────────────────────────────────────

def test_pnl_cash_buy():
    pnl, cash = pt._pnl_cash("BUY", 100.0, 120.0, 50)
    assert pnl == pytest.approx(1000.0)    # (exit - entry) * qty
    assert cash == pytest.approx(6000.0)   # +qty*exit (premium back on sell-to-close)


def test_pnl_cash_sell():
    pnl, cash = pt._pnl_cash("SELL", 100.0, 80.0, 50)
    assert pnl == pytest.approx(1000.0)    # (entry - exit) * qty
    assert cash == pytest.approx(-4000.0)  # -qty*exit (pay premium to buy-to-close)


def test_pnl_cash_loss_is_negative():
    pnl, _ = pt._pnl_cash("BUY", 100.0, 90.0, 50)
    assert pnl == pytest.approx(-500.0)


# ── the #1 invariant: a stop never flatters the trigger ──────────────────────

def test_long_stop_books_worse_than_trigger():
    entry, stop = 100.0, 90.0
    observed = 88.0  # premium gapped past the stop between 5s polls

    fill = pt._slip_fill(observed, "sell")
    assert fill <= stop      # never books the idealised stop level
    assert fill <= observed  # slippage is adverse on top of the gap

    realised, _ = pt._pnl_cash("BUY", entry, fill, 50)
    idealised, _ = pt._pnl_cash("BUY", entry, stop, 50)
    assert realised < idealised  # the realistic loss is strictly larger


def test_slip_explicit_override():
    # Per-book slippage overrides the module default.
    assert pt._slip_fill(100.0, "buy", 0.05) == pytest.approx(105.0)
    assert pt._slip_fill(100.0, "sell", 0.05) == pytest.approx(95.0)
    assert pt._slip_fill(100.0, "buy", 0.0) == 100.0


# ── size_by_risk: shared long/short lot sizing (pure) ────────────────────────

def _size(**kw):
    base = dict(equity=100000.0, per_unit_risk=10.0, lot_size=75, risk_pct=0.02,
                max_loss_per_trade=2000.0, risk_cap_enabled=True, min_lots=2, max_lots=10)
    base.update(kw)
    return pt.size_by_risk(**base)


def test_size_cap_on_normal():
    # one_lot_risk=750; cap=min(2000,2000)=2000 → floor(2000/750)=2 lots
    assert _size() == (2, None)


def test_size_cap_on_skips_when_one_lot_breaches():
    # per_unit_risk=40 → one_lot_risk=3000 > cap 2000 → skip
    assert _size(per_unit_risk=40.0) == (0, "risk_skip")


def test_size_cap_off_floors_at_min_lots():
    # tiny risk_pct → risk_lots=0, but min_lots floor applies
    assert _size(risk_cap_enabled=False, risk_pct=0.0001, min_lots=2) == (2, None)


def test_size_max_lots_ceiling():
    # cap off, huge risk_pct → ceiling at max_lots
    assert _size(risk_cap_enabled=False, risk_pct=1.0, max_lots=10) == (10, None)
