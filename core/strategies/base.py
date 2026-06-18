"""
base.py — the Strategy interface + shared order/position/context types.

A Strategy is driven IDENTICALLY by the backtester (core/backtest/engine.py) and,
later, the live paper loop — so an edge proven in backtest is the same code that
runs live. The Context abstracts WHERE option prices/strikes come from: in the
backtest they're synthesised from underlying + IV via Black-Scholes; in live they
come from the real Dhan chain. Strategies never call the pricer or the chain
directly — they ask the Context, so they don't care which world they're in.
"""
from dataclasses import dataclass, field


@dataclass
class OptionLeg:
    opt_type: str            # "CE" | "PE"
    strike: float
    side: str                # "SELL" | "BUY"
    lots: int = 1
    entry_premium: float = 0.0   # per-unit; filled by the engine/broker at open
    expiry_date: str = ""        # "YYYY-MM-DD"

    @property
    def sign(self) -> int:
        return 1 if self.side.upper() == "SELL" else -1  # +credit on sell, -debit on buy


@dataclass
class EquityLeg:
    symbol: str
    side: str                # "BUY" | "SELL"
    qty: int = 0
    entry_price: float = 0.0   # filled by the engine/broker at open

    @property
    def sign(self) -> int:
        return 1 if self.side.upper() == "BUY" else -1  # +1 long, −1 short


@dataclass
class Order:
    """A strategy's instruction to open a position. `kind` selects the economics:
    'option' (one or more OptionLeg) or 'equity' (symbol/side/qty)."""
    kind: str                       # "option" | "equity"
    underlying: str
    tag: str = ""                   # strategy-chosen id (dedupe / track)
    legs: list = field(default_factory=list)   # list[OptionLeg] for kind=="option"
    symbol: str = ""                # equity
    side: str = ""                  # equity: BUY/SELL
    qty: int = 0                    # equity
    meta: dict = field(default_factory=dict)


@dataclass
class Position:
    """An open position the engine/broker is tracking."""
    order: Order
    entry_date: str
    legs: list = field(default_factory=list)   # filled OptionLeg copies
    entry_value: float = 0.0        # net premium (credit +, debit −) per unit, options
    qty: int = 0                    # total units (lots × lot_size)
    margin: float = 0.0
    meta: dict = field(default_factory=dict)
    open: bool = True


@dataclass
class Action:
    """A management instruction from the strategy (currently: close)."""
    kind: str                       # "close"
    position: Position
    reason: str = ""


class Context:
    """Per-day market state handed to a strategy. Backtest/live subclasses implement
    `price_option` and `strike_for_delta`; everything else is plain data."""

    def __init__(self, date: str, spot: float, iv: float, *, iv_rank=None,
                 iv_percentile=None, realized_vol=None, r: float = 0.065,
                 underlying: str = "", step: int = 50):
        self.date = date
        self.spot = spot
        self.iv = iv                       # annualised vol FRACTION (e.g. 0.14)
        self.iv_rank = iv_rank             # 0..1 (None if unknown)
        self.iv_percentile = iv_percentile
        self.realized_vol = realized_vol
        self.r = r
        self.underlying = underlying       # e.g. "NIFTY"
        self.step = step                   # strike step for this underlying
        self.open_positions = []           # set by the engine each bar (read-only for strategies)

    def price_option(self, opt_type: str, strike: float, dte_days: int) -> float:
        raise NotImplementedError

    def strike_for_delta(self, opt_type: str, target_delta: float, dte_days: int) -> float:
        raise NotImplementedError


class Strategy:
    """Subclass and implement generate()/manage(). Both return lists; either may be
    empty. Strategies must be deterministic given the Context (no hidden clock/RNG)
    so backtest and live agree."""
    name = "base"

    def generate(self, ctx: Context) -> list:
        """Return Orders to OPEN on this bar (after applying the strategy's own
        entry filters, e.g. IV-rank gating). Empty list = do nothing."""
        return []

    def manage(self, positions: list, ctx: Context) -> list:
        """Return Actions for currently OPEN positions (e.g. close at 50% profit).
        The engine separately auto-settles at expiry."""
        return []
