"""
equity_engine.py — walk-forward simulator for equity / pairs strategies.

Separate from the options engine because the economics differ (cash long/short, no
greeks), but it shares the same Strategy interface, the same slippage model
(core/quant/costs) and the same go-live gate (core/backtest/metrics) — so a pairs
backtest is judged exactly like everything else.
"""
from datetime import date

from config.logger import get_logger
from config.settings import PAPER_COST_PER_TRADE, PAPER_SLIPPAGE_PCT, PAPER_START_CAPITAL
from core.quant.costs import slip_fill
from core.backtest.data import align
from core.backtest.engine import BacktestResult

log = get_logger("equity_engine")


class PairsContext:
    """Per-day state for a pair: both prices + the trailing close windows the
    strategy needs to compute hedge ratio / z-score / half-life."""

    def __init__(self, date, symbol_a, symbol_b, price_a, price_b, window_a, window_b):
        self.date = date
        self.symbol_a = symbol_a
        self.symbol_b = symbol_b
        self.price_a = price_a
        self.price_b = price_b
        self.window_a = window_a
        self.window_b = window_b
        self.open_positions = []

    def price(self, symbol: str) -> float:
        return self.price_a if symbol == self.symbol_a else self.price_b


def _days(d_from: str, d_to: str) -> int:
    return (date.fromisoformat(d_to[:10]) - date.fromisoformat(d_from[:10])).days


def _leg_pnl(leg, exit_price: float) -> float:
    """Per-leg P&L: long → (exit−entry)×qty, short → (entry−exit)×qty."""
    return leg.sign * (exit_price - leg.entry_price) * leg.qty


def run_pairs(strategy, bars_a: list, bars_b: list, *, symbol_a: str = "A",
              symbol_b: str = "B", start_capital: float = None,
              cost_per_trade: float = None, slippage_pct: float = None,
              lookback: int = 90) -> BacktestResult:
    """Drive a pairs Strategy over two aligned daily series. Equity legs fill at the
    bar close (± slippage); positions close on the strategy's manage() actions."""
    start_capital = PAPER_START_CAPITAL if start_capital is None else start_capital
    cost = PAPER_COST_PER_TRADE if cost_per_trade is None else cost_per_trade
    slip = PAPER_SLIPPAGE_PCT if slippage_pct is None else slippage_pct

    rows = align(bars_a, bars_b)
    positions, closed, curve = [], [], []
    realized = 0.0
    pos_seq = 0
    wa, wb = [], []

    def close_position(p, ctx, reason):
        nonlocal realized
        gross = 0.0
        for leg in p.legs:
            raw = ctx.price(leg.symbol)
            # exit is the opposite side: closing a long = sell (slip down), short = buy (slip up)
            exit_px = slip_fill(raw, "sell" if leg.side.upper() == "BUY" else "buy", slip)
            gross += _leg_pnl(leg, exit_px)
        net = gross - cost * len(p.legs)   # ~one round-trip cost per leg
        realized += net
        closed.append({"position_id": p.meta["pid"], "date": ctx.date,
                       "ts": ctx.date + " 15:30:00", "instrument": p.order.underlying,
                       "strategy": strategy.name, "pnl": round(net, 2), "reason": reason,
                       "entry_date": p.entry_date})
        p.open = False

    for (d, ba, bb) in rows:
        pa, pb = ba["close"], bb["close"]
        wa.append(pa); wb.append(pb)
        ctx = PairsContext(d, symbol_a, symbol_b, pa, pb, wa[-lookback:], wb[-lookback:])

        # manage existing
        ctx.open_positions = positions
        for a in (strategy.manage(positions, ctx) or []):
            if a.kind == "close" and a.position in positions and a.position.open:
                close_position(a.position, ctx, a.reason or "manage")
        positions = [p for p in positions if p.open]

        # new entries (strategy sees current open positions via ctx)
        ctx.open_positions = positions
        gross_used = sum(sum(abs(l.qty) * l.entry_price for l in p.legs) for p in positions)
        for order in (strategy.generate(ctx) or []):
            if order.kind != "equity_pair" or not order.legs:
                continue
            filled = []
            notional = 0.0
            for leg in order.legs:
                px = slip_fill(ctx.price(leg.symbol), "buy" if leg.side.upper() == "BUY" else "sell", slip)
                from core.strategies.base import EquityLeg
                filled.append(EquityLeg(leg.symbol, leg.side, leg.qty, px))
                notional += abs(leg.qty) * px
            if gross_used + notional > start_capital:
                continue  # gross-exposure cap
            pos_seq += 1
            from core.strategies.base import Position
            positions.append(Position(order=order, entry_date=d, legs=filled,
                                      entry_value=0.0, qty=sum(abs(l.qty) for l in filled),
                                      margin=notional, meta={"pid": pos_seq}))
            gross_used += notional

        # mark-to-market
        open_mtm = sum(sum(_leg_pnl(l, ctx.price(l.symbol)) for l in p.legs) for p in positions)
        curve.append((d, round(start_capital + realized + open_mtm, 2)))

    return BacktestResult(records=closed, equity_curve=curve,
                          start_capital=start_capital, underlying=f"{symbol_a}/{symbol_b}")
