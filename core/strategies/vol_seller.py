"""
vol_seller.py — VRP-harvesting iron-condor seller (Strategy interface).

The flagship edge: sell defined-risk index premium ONLY when implied vol is rich
(IV-rank ≥ threshold), collect the volatility risk premium, and manage mechanically
(take 50% of credit, stop at 2× credit). Defined-risk (long wings) caps the tail.

Runs identically in the backtester (BS-priced) and live (chain-priced) because it
only talks to the Context — never to a pricer or chain directly.
"""
from datetime import date, timedelta

from config.logger import get_logger
from config.settings import (
    SELLING_IV_RANK_MIN, SELLING_IV_RANK_MAX, SELLING_DELTA_TARGET, SELLING_SPREAD_WIDTH,
    SELLING_DTE, SELLING_EXIT_TAKE_PCT, SELLING_EXIT_STOP_MULTIPLE,
)
from core.strategies.base import Strategy, Order, OptionLeg, Action

log = get_logger("vol_seller")


class VolSeller(Strategy):
    name = "vol_seller"

    def __init__(self, iv_rank_min=None, iv_rank_max=None, target_delta=None, wing_pts=None,
                 wing_delta=None, dte=None, take_profit=None, stop_mult=None, lots=1):
        self.iv_rank_min = SELLING_IV_RANK_MIN if iv_rank_min is None else iv_rank_min
        self.iv_rank_max = SELLING_IV_RANK_MAX if iv_rank_max is None else iv_rank_max
        self.target_delta = SELLING_DELTA_TARGET if target_delta is None else target_delta
        self.wing_pts = SELLING_SPREAD_WIDTH if wing_pts is None else wing_pts
        # If set, place the long wing at THIS |delta| (further OTM than the short) instead
        # of a fixed point width — auto-scales across underlyings (NIFTY vs BANKNIFTY) and vol.
        self.wing_delta = wing_delta
        self.dte = SELLING_DTE if dte is None else dte
        self.take_profit = SELLING_EXIT_TAKE_PCT if take_profit is None else take_profit
        self.stop_mult = SELLING_EXIT_STOP_MULTIPLE if stop_mult is None else stop_mult
        self.lots = lots
        self._last_week = None

    def generate(self, ctx) -> list:
        # VRP gate: sell only when premium is rich (IV-rank ≥ min) but NOT in crisis
        # vol (IV-rank ≤ max) — the high-VIX tail is where short condors get run over.
        if ctx.iv_rank is not None and not (self.iv_rank_min <= ctx.iv_rank <= self.iv_rank_max):
            return []
        wk = date.fromisoformat(ctx.date).isocalendar()[:2]
        if self._last_week == wk:
            return []  # one condor per ISO week
        step = ctx.step or 50
        ce_short = ctx.strike_for_delta("CE", self.target_delta, self.dte)
        pe_short = ctx.strike_for_delta("PE", self.target_delta, self.dte)
        if self.wing_delta and 0 < self.wing_delta < self.target_delta:
            # delta-based wings scale with the underlying/vol (e.g. long the 10-delta)
            ce_wing = ctx.strike_for_delta("CE", self.wing_delta, self.dte)
            pe_wing = ctx.strike_for_delta("PE", self.wing_delta, self.dte)
        else:
            ce_wing = round((ce_short + self.wing_pts) / step) * step
            pe_wing = round((pe_short - self.wing_pts) / step) * step
        if pe_wing <= 0 or ce_wing <= ce_short or pe_wing >= pe_short:
            return []  # degenerate geometry — skip
        self._last_week = wk
        exp = (date.fromisoformat(ctx.date) + timedelta(days=self.dte)).isoformat()
        legs = [
            OptionLeg("CE", ce_short, "SELL", self.lots, expiry_date=exp),
            OptionLeg("CE", ce_wing, "BUY", self.lots, expiry_date=exp),
            OptionLeg("PE", pe_short, "SELL", self.lots, expiry_date=exp),
            OptionLeg("PE", pe_wing, "BUY", self.lots, expiry_date=exp),
        ]
        return [Order(kind="option", underlying=ctx.underlying or "NIFTY",
                      tag=f"condor-{ctx.date}", legs=legs)]

    def manage(self, positions, ctx) -> list:
        actions = []
        for p in positions:
            credit = p.entry_value           # net credit received per unit (>0)
            if credit <= 0 or not p.legs:
                continue
            dte = max((date.fromisoformat(p.legs[0].expiry_date)
                       - date.fromisoformat(ctx.date)).days, 0)
            close_cost = sum(leg.sign * ctx.price_option(leg.opt_type, leg.strike, dte)
                             for leg in p.legs)  # sold +cost to buy back, bought −credit
            profit = credit - close_cost
            if profit >= self.take_profit * credit:
                actions.append(Action("close", p, reason="take_profit"))
            elif profit <= -self.stop_mult * credit:
                actions.append(Action("close", p, reason="stop"))
        return actions
