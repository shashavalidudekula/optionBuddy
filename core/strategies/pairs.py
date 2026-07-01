"""
pairs.py — market-neutral statistical-arbitrage (pairs mean reversion).

Trade the spread between two cointegrated stocks back to its mean: when the spread
z-score stretches past a threshold, short the rich leg and long the cheap leg
(beta-weighted, so the book carries little market direction); close as it reverts.
Tradability is gated on correlation + a usable mean-reversion half-life, so we only
take pairs whose spread actually comes back. Backtest-first — OFF until it passes.
"""
from datetime import date

from config.logger import get_logger
from config.settings import (
    PAIRS_ENTRY_Z, PAIRS_EXIT_Z, PAIRS_STOP_Z, PAIRS_MAX_HALF_LIFE,
    PAIRS_MIN_CORR, PAIRS_LOOKBACK, PAIRS_MAX_DAYS, PAIRS_ALLOC,
)
from core.quant.stats import pair_signal
from core.strategies.base import Strategy, Order, Action, EquityLeg

log = get_logger("pairs")


class PairsStrategy(Strategy):
    name = "pairs"

    def __init__(self, symbol_a: str, symbol_b: str, entry_z=None, exit_z=None,
                 stop_z=None, max_half_life=None, min_corr=None, lookback=None,
                 max_days=None, alloc=None):
        self.a, self.b = symbol_a, symbol_b
        self.entry_z = PAIRS_ENTRY_Z if entry_z is None else entry_z
        self.exit_z = PAIRS_EXIT_Z if exit_z is None else exit_z
        self.stop_z = PAIRS_STOP_Z if stop_z is None else stop_z
        self.max_half_life = PAIRS_MAX_HALF_LIFE if max_half_life is None else max_half_life
        self.min_corr = PAIRS_MIN_CORR if min_corr is None else min_corr
        self.lookback = PAIRS_LOOKBACK if lookback is None else lookback
        self.max_days = PAIRS_MAX_DAYS if max_days is None else max_days
        self.alloc = PAIRS_ALLOC if alloc is None else alloc

    def generate(self, ctx) -> list:
        if ctx.open_positions:
            return []  # one position per pair at a time
        wa, wb = ctx.window_a, ctx.window_b
        if len(wa) < self.lookback or len(wb) < self.lookback:
            return []
        sig = pair_signal(wa, wb)
        hl = sig["half_life"]
        # tradability: well-correlated AND mean-reverting on a usable horizon
        if sig["corr"] < self.min_corr or sig["beta"] <= 0:
            return []
        if hl is None or hl <= 0 or hl > self.max_half_life:
            return []
        z, beta = sig["z"], sig["beta"]
        if abs(z) < self.entry_z:
            return []
        qa = int(self.alloc / ctx.price_a)
        qb = int(qa * beta)
        if qa < 1 or qb < 1:
            return []
        if z > 0:   # spread (a − β·b) rich → A overvalued → short A / long B
            legs = [EquityLeg(self.a, "SELL", qa), EquityLeg(self.b, "BUY", qb)]
        else:       # spread cheap → A undervalued → long A / short B
            legs = [EquityLeg(self.a, "BUY", qa), EquityLeg(self.b, "SELL", qb)]
        return [Order(kind="equity_pair", underlying=f"{self.a}/{self.b}",
                      tag=f"pair-{ctx.date}", legs=legs, meta={"entry_z": z, "beta": beta})]

    def manage(self, positions, ctx) -> list:
        if not positions:
            return []
        z = pair_signal(ctx.window_a, ctx.window_b)["z"]
        actions = []
        for p in positions:
            held = (date.fromisoformat(ctx.date) - date.fromisoformat(p.entry_date)).days
            if abs(z) <= self.exit_z:
                actions.append(Action("close", p, "mean_revert"))
            elif abs(z) >= self.stop_z:
                actions.append(Action("close", p, "stop"))
            elif held >= self.max_days:
                actions.append(Action("close", p, "time"))
        return actions
