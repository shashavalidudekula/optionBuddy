"""margin.py — approximate option margin for the selling paper books.

The long-only paper cash model never blocked margin (a long just deploys premium).
Selling does: a naked short blocks SPAN+exposure margin; a defined-risk spread blocks
≈ its max loss. These are APPROXIMATIONS for paper sizing only — exchange margins move
and vary intraday, so CALIBRATE against Dhan's margin calculator before trusting capital
adequacy (see GO_LIVE_CRITERIA.md). Functions take explicit overrides so they're pure
and unit-testable without the settings/env.

Conventions: index points == premium points (1 pt = ₹1 per unit); a "unit" is one
quantity, a "lot" is `lot_size` units. Rupee margin = per-unit figure × lot_size × lots.
"""
from config.logger import get_logger
from config.settings import NAKED_MARGIN_PER_LOT, MARGIN_DEFAULT_PER_LOT

log = get_logger("margin")


def naked_short_margin(underlying: str, lots: int, per_lot: float | None = None) -> float:
    """Rupee margin to block for a naked short of `lots` lots on `underlying`.

    `per_lot` overrides the configured per-lot estimate (for tests / calibration).
    """
    if lots <= 0:
        return 0.0
    if per_lot is None:
        per_lot = NAKED_MARGIN_PER_LOT.get(str(underlying).upper(), MARGIN_DEFAULT_PER_LOT)
    return round(per_lot * lots, 2)


def spread_margin(width_points: float, net_credit: float, lot_size: int, lots: int) -> float:
    """Rupee margin (= max loss) for a defined-risk credit spread.

    width_points: strike distance between the short and the long (protective) leg.
    net_credit:   credit received per unit (short premium − long premium), in points.
    Max loss per unit = max(width − credit, 0); margin = that × lot_size × lots, floored at 0.
    """
    if lots <= 0 or lot_size <= 0:
        return 0.0
    max_loss_per_unit = max(float(width_points) - float(net_credit), 0.0)
    return round(max_loss_per_unit * lot_size * lots, 2)


def max_affordable_lots(free_margin: float, underlying: str, *, structure: str = "naked",
                        width_points: float = 0.0, net_credit: float = 0.0,
                        lot_size: int = 0, per_lot: float | None = None) -> int:
    """Largest whole lot count whose margin fits within `free_margin`.

    structure='naked' uses the per-lot estimate; 'spread' uses the max-loss formula.
    Returns 0 when not even one lot fits.
    """
    if free_margin <= 0:
        return 0
    if structure == "spread":
        one = spread_margin(width_points, net_credit, lot_size, 1)
    else:
        one = naked_short_margin(underlying, 1, per_lot=per_lot)
    if one <= 0:
        return 0
    return int(free_margin // one)
