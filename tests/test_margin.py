"""Tests for the option-selling margin model (paper books B)."""
import pytest

from core import margin as m


# ── naked short margin ───────────────────────────────────────────────────────

def test_naked_uses_explicit_per_lot():
    assert m.naked_short_margin("NIFTY", 2, per_lot=110000) == pytest.approx(220000.0)


def test_naked_zero_or_negative_lots():
    assert m.naked_short_margin("NIFTY", 0, per_lot=110000) == 0.0
    assert m.naked_short_margin("NIFTY", -3, per_lot=110000) == 0.0


def test_naked_unknown_underlying_falls_back_to_default():
    # No per_lot override → unknown symbol uses the configured default.
    v = m.naked_short_margin("WHO_DIS", 1)
    assert v == pytest.approx(m.MARGIN_DEFAULT_PER_LOT)


# ── defined-risk spread margin (= max loss) ──────────────────────────────────

def test_spread_margin_is_capped_max_loss():
    # 50-pt wide NIFTY spread, ₹20 credit, lot 75 → (50-20)*75 = 2250 per lot
    assert m.spread_margin(50, 20, 75, 1) == pytest.approx(2250.0)
    assert m.spread_margin(50, 20, 75, 3) == pytest.approx(6750.0)


def test_spread_margin_never_negative():
    # Credit wider than the spread can't make margin negative (floored at 0).
    assert m.spread_margin(50, 80, 75, 2) == 0.0


def test_spread_margin_far_less_than_naked():
    # The whole point of spreads: margin is a fraction of naked.
    spread = m.spread_margin(100, 30, 75, 1)
    naked = m.naked_short_margin("NIFTY", 1, per_lot=110000)
    assert spread < naked


# ── affordability ────────────────────────────────────────────────────────────

def test_max_affordable_naked():
    assert m.max_affordable_lots(250000, "NIFTY", per_lot=110000) == 2  # 2*110k=220k fits, 3rd doesn't


def test_max_affordable_spread():
    # one spread lot = (50-20)*75 = 2250; 10000 free → 4 lots
    assert m.max_affordable_lots(10000, "NIFTY", structure="spread",
                                 width_points=50, net_credit=20, lot_size=75) == 4


def test_max_affordable_none_when_broke():
    assert m.max_affordable_lots(0, "NIFTY", per_lot=110000) == 0
    assert m.max_affordable_lots(50000, "NIFTY", per_lot=110000) == 0  # < 1 lot
