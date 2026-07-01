"""
engine.py — walk-forward backtest simulator.

Drives a Strategy (core/strategies/base.py) over a daily history. Options are
priced synthetically via Black-Scholes using India-VIX as the implied vol and
settled at realised intrinsic — so the simulated P&L of systematic SELLING is
exactly the volatility-risk-premium harvest (implied collected vs. realised paid).

Economics match live by construction: the same slippage model (paper_trader
`_slip_fill`) on market exits, a flat per-trade cost, and the margin model
(core/margin) for capital limits. Output records feed the SAME go-live gate the
live paper account is judged by (scripts/go_live_readiness.evaluate).

NOTE (honest caveat): single ATM IV (no skew), theoretical fills. This RANKS
strategies and estimates the edge; paper validation after a backtest pass stays
mandatory before live.
"""
import math
from dataclasses import dataclass, field

from config.logger import get_logger
from config.settings import PAPER_COST_PER_TRADE, PAPER_SLIPPAGE_PCT, PAPER_START_CAPITAL
from core.quant.pricing import bs_price, bs_greeks
from core.quant import vol as volmod
from core.quant.costs import slip_fill
from core.margin import naked_short_margin
from core.strategies.base import Context, Position, OptionLeg

log = get_logger("backtest_engine")

# Approximate current NSE lot sizes / strike steps for the index underlyings.
# (Lots have changed over time; this is a backtest approximation — documented.)
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65, "MIDCPNIFTY": 140,
            "SENSEX": 20, "BANKEX": 30}
ATM_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25,
            "SENSEX": 100, "BANKEX": 100}


def lot_size(underlying: str) -> int:
    return LOT_SIZE.get(str(underlying).upper(), 50)


def strike_step(underlying: str) -> int:
    return ATM_STEP.get(str(underlying).upper(), 50)


def _intrinsic(opt_type: str, K: float, ST: float) -> float:
    return max(ST - K, 0.0) if str(opt_type).upper() == "CE" else max(K - ST, 0.0)


def _days_between(d_from: str, d_to: str) -> int:
    from datetime import date
    a = date.fromisoformat(d_from[:10]); b = date.fromisoformat(d_to[:10])
    return (b - a).days


# ── synthetic market context for the backtest ────────────────────────────────

class BacktestContext(Context):
    """Context whose option prices/strikes come from Black-Scholes on (spot, iv)."""

    def __init__(self, underlying: str, step: int, **kw):
        super().__init__(**kw)
        self.underlying = underlying
        self.step = step

    def price_option(self, opt_type: str, strike: float, dte_days: int) -> float:
        return bs_price(self.spot, strike, max(dte_days, 0) / 365.0, self.r, self.iv, opt_type)

    def strike_for_delta(self, opt_type: str, target_delta: float, dte_days: int) -> float:
        T = max(dte_days, 1) / 365.0
        atm = round(self.spot / self.step) * self.step
        best, best_err = atm, 1e9
        for i in range(-80, 81):
            K = atm + i * self.step
            if K <= 0:
                continue
            d = abs(bs_greeks(self.spot, K, T, self.r, self.iv, opt_type)["delta"])
            err = abs(d - target_delta)
            if err < best_err:
                best, best_err = K, err
        return best


# ── position economics ───────────────────────────────────────────────────────

def _net_credit_per_unit(legs: list) -> float:
    """Cash received at open per unit (sell +premium, buy −premium)."""
    return sum(leg.sign * leg.entry_premium for leg in legs)


def _is_defined_risk(legs: list) -> bool:
    sold_ce = [l for l in legs if l.side.upper() == "SELL" and l.opt_type.upper() == "CE"]
    sold_pe = [l for l in legs if l.side.upper() == "SELL" and l.opt_type.upper() == "PE"]
    bought_ce = [l for l in legs if l.side.upper() == "BUY" and l.opt_type.upper() == "CE"]
    bought_pe = [l for l in legs if l.side.upper() == "BUY" and l.opt_type.upper() == "PE"]
    for s in sold_ce:
        if not any(b.strike > s.strike for b in bought_ce):
            return False
    for s in sold_pe:
        if not any(b.strike < s.strike for b in bought_pe):
            return False
    return bool(sold_ce or sold_pe)


def _position_margin(legs, net_credit, lots, spot, underlying) -> float:
    """Defined-risk → max loss via expiry payoff scan; naked → config SPAN margin."""
    ls = lot_size(underlying)
    if _is_defined_risk(legs):
        worst = 0.0
        for i in range(0, 61):
            ST = spot * (0.5 + i * (1.0 / 60.0))  # 0.5x .. 1.5x spot
            pnl = net_credit - sum(leg.sign * _intrinsic(leg.opt_type, leg.strike, ST) for leg in legs)
            worst = min(worst, pnl)
        return max(-worst, 0.0) * ls * lots
    return naked_short_margin(underlying, lots)


def _close_value_per_unit(legs, ctx, dte_remaining, slip, at_expiry) -> float:
    """P&L per unit of closing all legs now (gross of cost)."""
    g = 0.0
    for leg in legs:
        if at_expiry:
            exit_prem = _intrinsic(leg.opt_type, leg.strike, ctx.spot)  # cash settle, no slip
        else:
            raw = ctx.price_option(leg.opt_type, leg.strike, dte_remaining)
            side = "buy" if leg.side.upper() == "SELL" else "sell"
            exit_prem = slip_fill(raw, side, slip)
        if leg.side.upper() == "SELL":
            g += leg.entry_premium - exit_prem
        else:
            g += exit_prem - leg.entry_premium
    return g


@dataclass
class BacktestResult:
    records: list = field(default_factory=list)     # closed-trade records (feed the gate)
    equity_curve: list = field(default_factory=list)  # [(date, equity)]
    start_capital: float = 0.0
    underlying: str = ""

    def summary(self) -> dict:
        nets = [r["pnl"] for r in self.records]
        n = len(nets)
        if n == 0:
            return {"trades": 0}
        wins = [x for x in nets if x > 0]
        total = sum(nets)
        peak = cum = dd = 0.0
        for x in nets:
            cum += x; peak = max(peak, cum); dd = max(dd, peak - cum)
        mu = total / n
        sd = (sum((x - mu) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else 0.0
        return {
            "trades": n, "net_pnl": round(total, 2), "expectancy": round(mu, 2),
            "win_rate": round(100.0 * len(wins) / n, 1),
            "profit_factor": round(sum(wins) / abs(sum(x for x in nets if x < 0)), 2)
                             if any(x < 0 for x in nets) else math.inf,
            "max_drawdown": round(dd, 2),
            "return_pct": round(total / self.start_capital * 100, 2) if self.start_capital else 0.0,
            "t_stat": round(mu / (sd / math.sqrt(n)), 2) if sd > 0 and n > 1 else 0.0,
        }


# ── the simulator ─────────────────────────────────────────────────────────────

def run(strategy, underlying_bars: list, vix_bars: list, *, underlying: str = "NIFTY",
        start_capital: float = None, cost_per_trade: float = None,
        slippage_pct: float = None, iv_lookback: int = 252,
        rvol_window: int = 20, r: float = 0.065) -> BacktestResult:
    """Walk `underlying_bars` (daily {date,ohlc}) aligned with `vix_bars`, driving
    `strategy`. Options priced/settled synthetically. Returns a BacktestResult."""
    from core.backtest.data import align

    start_capital = PAPER_START_CAPITAL if start_capital is None else start_capital
    cost = PAPER_COST_PER_TRADE if cost_per_trade is None else cost_per_trade
    slip = PAPER_SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    step = strike_step(underlying)

    rows = align(underlying_bars, vix_bars)
    positions: list = []
    closed: list = []
    curve: list = []
    realized_cum = 0.0
    pos_seq = 0
    vix_hist: list = []
    close_hist: list = []

    def record_close(p: Position, net_pnl: float, exit_date: str, reason: str):
        nonlocal realized_cum
        realized_cum += net_pnl
        closed.append({
            # monotonic id, NOT id(p): Python recycles object ids after GC, which
            # would merge distinct round trips in the gate's position grouping.
            "position_id": p.meta.get("pid"), "date": exit_date, "ts": exit_date + " 15:30:00",
            "instrument": p.order.tag or p.order.underlying, "strategy": strategy.name,
            "pnl": round(net_pnl, 2), "reason": reason, "entry_date": p.entry_date,
        })
        p.open = False

    for (d, ubar, vbar) in rows:
        spot = ubar["close"]
        vix = vbar["close"]
        iv = vix / 100.0
        vix_hist.append(vix); close_hist.append(spot)
        vh = vix_hist[-iv_lookback:]
        ch = close_hist[-(rvol_window + 1):]
        ctx = BacktestContext(
            underlying, step, date=d, spot=spot, iv=iv,
            iv_rank=volmod.iv_rank(vix, vh), iv_percentile=volmod.iv_percentile(vix, vh),
            realized_vol=volmod.realized_vol(ch), r=r)

        # 1) settle expiries (cash, no slip)
        survivors = []
        for p in positions:
            exp = p.legs[0].expiry_date if p.legs else d
            if exp and d >= exp:
                gross = _close_value_per_unit(p.legs, ctx, 0, slip, at_expiry=True) * p.qty
                record_close(p, gross - cost, d, "expiry")
            else:
                survivors.append(p)
        positions = survivors

        # 2) strategy-driven management (market exits, slipped)
        for a in (strategy.manage(positions, ctx) or []):
            p = a.position
            if p not in positions or not p.open:
                continue
            dte = max(_days_between(d, p.legs[0].expiry_date), 0) if p.legs else 0
            gross = _close_value_per_unit(p.legs, ctx, dte, slip, at_expiry=False) * p.qty
            record_close(p, gross - cost, d, a.reason or "manage")
        positions = [p for p in positions if p.open]

        # 3) new entries
        used_margin = sum(p.margin for p in positions)
        for order in (strategy.generate(ctx) or []):
            if order.kind != "option" or not order.legs:
                continue
            ls = lot_size(underlying)
            lots = max(int(order.legs[0].lots), 1)
            filled = []
            for leg in order.legs:
                dte = max(_days_between(d, leg.expiry_date), 1) if leg.expiry_date else 7
                prem = ctx.price_option(leg.opt_type, leg.strike, dte)
                # entry slips adversely: sell-to-open receives less, buy-to-open pays more
                prem = slip_fill(prem, "sell" if leg.side.upper() == "SELL" else "buy", slip)
                filled.append(OptionLeg(leg.opt_type, leg.strike, leg.side, lots, prem, leg.expiry_date))
            net_credit = _net_credit_per_unit(filled)
            margin = _position_margin(filled, net_credit, lots, spot, underlying)
            if used_margin + margin > start_capital:
                continue  # capital limit
            qty = lots * ls
            pos_seq += 1
            positions.append(Position(order=order, entry_date=d, legs=filled,
                                      entry_value=net_credit, qty=qty, margin=margin,
                                      meta={"pid": pos_seq}))
            used_margin += margin

        # 4) mark-to-market equity point
        open_mtm = sum(_close_value_per_unit(p.legs, ctx,
                       max(_days_between(d, p.legs[0].expiry_date), 0) if p.legs else 0,
                       0.0, at_expiry=False) * p.qty for p in positions)
        curve.append((d, round(start_capital + realized_cum + open_mtm, 2)))

    return BacktestResult(records=closed, equity_curve=curve,
                          start_capital=start_capital, underlying=underlying)
