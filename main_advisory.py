"""
main_advisory.py — Orchestrator for the OptionBuddy advisory product.

Advisory-only: it researches the market, publishes trade-idea "calls" (entry,
target(s), stop-loss + rationale) and tracks each call's lifecycle, pushing
Telegram alerts to subscribers. There is NO order execution and NO broker link.

Loop responsibilities (fast loop every POLL_INTERVAL_SEC, ~5s):
  • Startup       — init advisory DB, start the Telegram bot polling.
  • Tracking      — every poll, evaluate active calls against live prices and
                    push entry/target/SL/expiry events (this is the near-real-time
                    layer: it fires the instant a premium hits its entry/exit level).
  • Option gen    — EVENT-DRIVEN: re-scan index options the moment the market moves
                    (index % move, new ATM strike, VIX jump) or at least every
                    OPT_GEN_FLOOR_SEC, with an OPT_GEN_MIN_GAP_SEC cooldown.
  • Other gen     — equity/futures/commodity on a slower OTHER_GEN_INTERVAL_MIN timer.
  • Briefing      — once each trading morning, broadcast a "good morning" note.
  • EOD summary   — once after close, broadcast the day's track-record digest.

All heavy/blocking work (LLM, yfinance, news, DB) runs in worker threads so the
Telegram event loop stays responsive.
"""

import asyncio
import time
from datetime import datetime, time as dtime

from config.logger import get_logger
from config.settings import (
    POLL_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE, PAPER_TRADING_ENABLED,
    PAPER_RESET_ON_START, PAPER_START_CAPITAL, PAPER_WEEKLY_RESET,
    OPT_GEN_MIN_GAP_SEC, OPT_GEN_FLOOR_SEC, GEN_MOVE_PCT, GEN_VIX_JUMP_PCT,
    OTHER_GEN_INTERVAL_MIN, ATM_STEP, EOD_SQUARE_OFF_ALL,
    GEN_HALT_TIME, EOD_CLOSE_TIME, EOD_DIGEST_TIME, GEN_RESUME_TIME,
    OPENING_SCALP_ENABLED, FEED_ALERTS_ENABLED, FEED_STALE_SEC,
    EXECUTION_MODE, RECONCILE_ENABLED, RECONCILE_INTERVAL_SEC,
    SELLING_ENABLED, SELLING_UNDERLYINGS, SELLING_STRUCTURE,
    EQUITY_FACTOR_ENABLED, EQUITY_FACTOR_UNIVERSE,
    STOCK_OPT_ENABLED, STOCK_OPT_UNDERLYINGS,
)
from data.advisory_store import (
    CATEGORIES,
    DEFAULT_STRATEGY,
    init_advisory_db,
    save_call,
    active_instruments,
    get_track_record,
    get_active_calls,
    get_paper_account,
    reset_paper_account,
)
from signals.advisory_engine import generate_calls
from core.call_tracker import (
    track_active_calls, sweep_stale_calls, force_close_all_calls, cancel_waiting_calls,
)
from core.paper_trader import PaperTrader
from core.opening_scalp import OpeningScalp
from core.option_seller import OptionSeller
from core.equity_factor import EquityFactor
from core.stock_options import StockOptions
from core.execution import get_broker
from core import heartbeat
from core.market_data_provider import (
    get_session,
    get_market_snapshot, make_price_lookup, get_option_chain, get_index_spots,
    option_expiry_for,
)
from data.news_fetcher import get_top_headlines
from notifier.telegram_advisory_bot import (
    TelegramAdvisoryBot,
    CATEGORY_LABEL,
    DISCLAIMER,
)

log = get_logger("advisory_main")

# Don't start broadcasting the morning briefing before this time.
BRIEFING_AFTER = dtime(8, 30)
# Pre-market scan at 9:08 AM (market opens at 9:15, pre-market closes at 9:08).
# This gives 7 minutes to prepare for opening-level breakouts.
PREMARKET_SCAN = dtime(9, 8)
def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


_PREMARKET_T = PREMARKET_SCAN
_OPEN_T = _parse_hhmm(MARKET_OPEN)
_CLOSE_T = _parse_hhmm(MARKET_CLOSE)

# EOD close-out schedule (configurable via .env; see config/settings.py):
#   GEN_HALT_AFTER  — stop generating + cancel waiting calls (default 15:28)
#   EOD_CLOSE_AFTER — square off all in-trade calls/positions (default 15:29)
#   EOD_AFTER       — broadcast the EOD digest (default 15:35)
# Halting before the square-off ensures nothing fresh can trigger and carry overnight.
GEN_HALT_AFTER = _parse_hhmm(GEN_HALT_TIME)
EOD_CLOSE_AFTER = _parse_hhmm(EOD_CLOSE_TIME)
EOD_AFTER = _parse_hhmm(EOD_DIGEST_TIME)
# Normal (LLM) generation is suppressed until this time (default = open, no effect).
GEN_RESUME_AFTER = _parse_hhmm(GEN_RESUME_TIME)


def _is_weekday(now: datetime) -> bool:
    return now.weekday() < 5  # Mon–Fri


def _in_market_hours(now: datetime) -> bool:
    return _is_weekday(now) and _OPEN_T <= now.time() <= _CLOSE_T


# ── Work units (sync, run in threads) ─────────────────────────────────────────

def _generate_all_categories(session, categories, indices=None) -> list[tuple[dict, int]]:
    """Generate + persist fresh calls for the given categories. Returns (call, id).

    `indices` restricts the option_chain feed to the index(es) that actually
    triggered the scan (token saving); None → all tracked indices (pre-market).
    """
    headlines = get_top_headlines()
    market = get_market_snapshot(session)
    exclude = active_instruments()
    idx_list = list(indices) if indices else ["NIFTY", "BANKNIFTY", "SENSEX"]

    saved: list[tuple[dict, int]] = []
    for cat in categories:
        cat_market = market
        if cat == "index_option" and session is not None:
            # Ground option calls in real premiums — only for the index(es) that
            # moved, so we don't ship 3 full chains every scan.
            try:
                cat_market = {
                    **market,
                    "option_chain": {u: get_option_chain(session, u) for u in idx_list},
                }
            except Exception as e:
                log.warning("Option-chain fetch failed: %s", e)
        try:
            calls = generate_calls(cat, cat_market, headlines, exclude_instruments=exclude)
        except Exception as e:
            log.error("Generation failed for %s: %s", cat, e)
            continue
        for call in calls:
            # Stamp the option's real contract expiry (for display + EOD settlement).
            if cat == "index_option" and session is not None:
                try:
                    exp = option_expiry_for(session, call.get("underlying", ""), call.get("instrument", ""))
                    if exp:
                        call["option_expiry"] = exp.isoformat()
                except Exception as e:  # noqa: BLE001
                    log.debug("Expiry resolve failed (%s): %s", call.get("instrument"), e)
            try:
                call_id = save_call(call)
            except Exception as e:
                log.error("save_call failed (%s): %s", call.get("instrument"), e)
                continue
            saved.append((call, call_id))
            try:  # audit the inputs + rationale behind this call (for /why)
                from data.decision_log import log_decision
                log_decision(call_id, call, cat_market, headlines)
            except Exception as e:  # noqa: BLE001
                log.debug("Decision log skipped (%s): %s", call.get("instrument"), e)
            exclude.add(call.get("instrument"))  # avoid dupes within this cycle
    return saved


# ── Async cycles ──────────────────────────────────────────────────────────────

async def run_generation_cycle(bot: TelegramAdvisoryBot, session, categories, indices=None) -> None:
    saved = await asyncio.to_thread(_generate_all_categories, session, categories, indices)
    for call, call_id in saved:
        await bot.push_new_call(call, call_id)
    if saved:
        log.info("Generation (%s) published %s call(s)", ",".join(categories), len(saved))


class OptionGenTrigger:
    """Adaptive time-based scanning for options — maximum responsiveness to reversals.

    Scanning intervals (time-of-day aware):
      9:15–9:45 AM: 3 sec    (peak opening volatility, catch reversals)
      9:45–10:30 AM: 20 sec   (still volatile, need quick response)
      10:30 AM–12:00 PM: 45 sec  (mid-morning, lower volatility)
      12:00–1:00 PM: 30 sec   (lunch volatility spike)
      1:00–3:30 PM: 15 sec    (afternoon session, moderate volatility)

    Reversal trigger: 0.25% (immediate rescan if market reverses)

    Prevents stale calls at all times. Example: 9:15 bearish scan → 9:16 market
    reverses 0.25% → immediate 9:16:05 rescan sees bullish setup → buys CALLS.
    """

    def __init__(self):
        self.last_gen = 0.0
        self.ref_spot: dict[str, float] = {}
        self.ref_atm: dict[str, float] = {}
        self.ref_vix: float | None = None
        self.market_open_scanned = False
        self.reversal_threshold = 0.25  # 0.25% reversal triggers immediate rescan

    @staticmethod
    def _atm(index: str, spot: float | None) -> float | None:
        step = ATM_STEP.get(index)
        if not step or not spot:
            return None
        return round(spot / step) * step

    @staticmethod
    def _get_scan_interval(now: datetime) -> float:
        """Return scan interval in seconds based on time of day (market volatility)."""
        hour = now.hour
        minute = now.minute
        time_mins = hour * 60 + minute

        # 9:15–9:45 AM: 3 sec (peak opening volatility)
        if dtime(9, 15) <= now.time() <= dtime(9, 45):
            return 3
        # 9:45–10:30 AM: 20 sec (still volatile)
        elif dtime(9, 45) < now.time() <= dtime(10, 30):
            return 20
        # 10:30 AM–12:00 PM: 45 sec (mid-morning)
        elif dtime(10, 30) < now.time() <= dtime(12, 0):
            return 45
        # 12:00–1:00 PM: 30 sec (lunch volatility)
        elif dtime(12, 0) < now.time() <= dtime(13, 0):
            return 30
        # 1:00–3:30 PM: 15 sec (afternoon)
        elif dtime(13, 0) < now.time() <= dtime(15, 30):
            return 15
        # Default fallback (outside trading hours)
        else:
            return 60

    def check(self, spots: dict) -> tuple[bool, str, set | None]:
        """Return (fire, reason, indices). `indices` is the set of indices that
        triggered (so generation can feed only their chains); None → all."""
        now = datetime.now()
        now_ts = time.time()

        # Force scan at market open (9:15 AM) to catch early breakouts
        if (not self.market_open_scanned and now.time() >= _OPEN_T
                and not self.ref_spot):
            return True, "market-open", None

        # Adaptive time-based scanning interval (faster during volatile hours)
        scan_interval = self._get_scan_interval(now)
        if now_ts - self.last_gen < scan_interval:
            return False, "", None

        if not self.ref_spot:
            return True, "init", None

        reasons: list[str] = []
        triggered: set[str] = set()

        # Check for reversals (0.25% threshold) — trigger immediate rescan.
        # Example: 9:15 bearish scan, 9:16 market reverses 0.25% → rescan immediately.
        for lbl, idx in (("nifty", "NIFTY"), ("banknifty", "BANKNIFTY"), ("sensex", "SENSEX")):
            cur, ref = spots.get(lbl), self.ref_spot.get(lbl)
            if cur and ref:
                pct_move = abs(cur - ref) / ref * 100
                # Reversal: moved 0.25%+ in any direction
                if pct_move >= self.reversal_threshold:
                    reasons.append(f"{idx} reversal {pct_move:+.2f}%")
                    triggered.add(idx)
                # Regular movement trigger
                if pct_move >= GEN_MOVE_PCT:
                    reasons.append(f"{idx} move {(cur - ref) / ref * 100:+.2f}%")
                    triggered.add(idx)
            # ATM strike changed
            atm, ratm = self._atm(idx, cur), self.ref_atm.get(lbl)
            if atm and ratm and atm != ratm:
                reasons.append(f"{idx} ATM→{int(atm)}")
                triggered.add(idx)

        vix = spots.get("indiavix")
        if vix and self.ref_vix and abs(vix - self.ref_vix) / self.ref_vix * 100 >= GEN_VIX_JUMP_PCT:
            reasons.append(f"VIX {(vix - self.ref_vix) / self.ref_vix * 100:+.1f}%")
            triggered |= {"NIFTY", "BANKNIFTY", "SENSEX"}  # VIX moves all

        return (bool(reasons), ", ".join(reasons), triggered or None)

    def commit(self, spots: dict) -> None:
        self.last_gen = time.time()
        self.market_open_scanned = True
        for lbl, idx in (("nifty", "NIFTY"), ("banknifty", "BANKNIFTY"), ("sensex", "SENSEX")):
            if spots.get(lbl):
                self.ref_spot[lbl] = spots[lbl]
                self.ref_atm[lbl] = self._atm(idx, spots[lbl])
        if spots.get("indiavix"):
            self.ref_vix = spots["indiavix"]


async def run_tracking_pass(bot: TelegramAdvisoryBot, price_lookup, traders: dict[str, PaperTrader]) -> None:
    events = await asyncio.to_thread(track_active_calls, price_lookup)
    for evt in events:
        await bot.push_call_event(evt)
    if events:
        log.info("Tracking pass emitted %s event(s)", len(events))

    # Shadow/paper trading reacts to the same lifecycle events (zero real money).
    # Each trader processes only its strategy's events (routed by call.strategy field).
    for strategy, trader in traders.items():
        await asyncio.to_thread(trader.mark_to_market, price_lookup)
        notes = await asyncio.to_thread(trader.process_events, events)
        for note in notes:
            await bot.notify_owner(note)


def _track_record_lines(title: str) -> list[str]:
    rec = get_track_record(days=30)
    o = rec["overall"]
    lines = [
        title,
        "",
        f"Calls closed (30d): *{o['total']}*",
        f"Win rate: *{o['win_rate']}%*  (avg {o['avg_return']}%/call)",
    ]
    by_cat = [
        f"• {CATEGORY_LABEL.get(c, c)}: {s['win_rate']}% ({s['wins']}/{s['total']})"
        for c, s in rec["by_category"].items() if s["total"]
    ]
    if by_cat:
        lines += ["", *by_cat]
    return lines


async def send_morning_briefing(bot: TelegramAdvisoryBot) -> None:
    active = await asyncio.to_thread(get_active_calls)
    lines = [
        "☀️ *Good morning from OptionBuddy!*",
        "",
        f"{len(active)} call(s) still live from before. We'll scan Index Options, "
        "Equity, Futures & Commodity through the day and alert you on every fresh "
        "setup and every target/stop-loss.",
        "",
        *_track_record_lines("📊 *Our last 30 days*"),
        "",
        DISCLAIMER,
    ]
    await bot.broadcast("\n".join(lines))
    log.info("Morning briefing broadcast")


async def send_eod_summary(bot: TelegramAdvisoryBot) -> None:
    lines = [
        "🌙 *Market closed — daily wrap*",
        "",
        *_track_record_lines("Here's how our calls are tracking:"),
        "",
        "Rest up — fresh setups tomorrow. " + DISCLAIMER,
    ]
    await bot.broadcast("\n".join(lines))
    log.info("EOD summary broadcast")


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run() -> None:
    init_advisory_db()
    log.info("Advisory DB ready")

    # One-time destructive reset of the paper account (set the flag for one boot).
    if PAPER_RESET_ON_START:
        reset_paper_account(PAPER_START_CAPITAL)
        log.warning("PAPER_RESET_ON_START=true — paper account reset to ₹%.0f. "
                    "Set it back to false to avoid resetting on the next boot.",
                    PAPER_START_CAPITAL)

    # Live market-data feed (provider chosen by MARKET_DATA_PROVIDER; data only).
    try:
        session = get_session()
    except Exception as e:
        session = None
        log.error("Market-data session unavailable (%s). Running with global cues only; "
                  "option/equity tracking will rely on expiry until credentials are set.", e)

    # Let the technicals/tape layer compute on official Dhan candles (reusing this
    # one session — no second token). Falls back to yfinance if the session is None.
    try:
        from core.technicals import set_data_session
        set_data_session(session)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not register technicals data session: %s", e)

    bot = TelegramAdvisoryBot()
    await bot.start_polling()

    price_lookup = make_price_lookup(session)

    # Broker routes orders only in EXECUTION_MODE=live (and still double-guarded);
    # in paper mode it's a no-op. Captured here so reconciliation can read its book.
    broker = get_broker(session)

    # Multi-strategy paper books: one PaperTrader per strategy. Each is capital-separated,
    # margin-tracked, and reports independently.
    traders: dict[str, PaperTrader] = {}
    if PAPER_TRADING_ENABLED:
        # 1. opt_buy: index option buying (existing engine, with LLM)
        try:
            traders[DEFAULT_STRATEGY] = PaperTrader(session, broker=broker, strategy=DEFAULT_STRATEGY,
                                                   allow_buy=True, allow_sell=False)
        except Exception as e:
            log.error("Paper trader [opt_buy] init failed (%s); continuing without it.", e)

        # 2. opt_sell_spread: defined-risk credit spreads (deterministic, no LLM)
        if SELLING_ENABLED:
            try:
                traders["opt_sell_spread"] = PaperTrader(session, broker=broker, strategy="opt_sell_spread",
                                                        capital=300000, allow_buy=False, allow_sell=True)
            except Exception as e:
                log.error("Paper trader [opt_sell_spread] init failed (%s); continuing without it.", e)

        # 3. opt_sell_naked: naked shorts (deterministic, no LLM, higher risk/reward)
        if SELLING_ENABLED and SELLING_STRUCTURE.lower() == "naked":
            try:
                traders["opt_sell_naked"] = PaperTrader(session, broker=broker, strategy="opt_sell_naked",
                                                       capital=500000, allow_buy=False, allow_sell=True)
            except Exception as e:
                log.error("Paper trader [opt_sell_naked] init failed (%s); continuing without it.", e)

        # 4. stock_opt: stock options (minimal LLM, illiquid, buying only). Its calls are
        #    tagged category="stock_option", so the trader must scope to that — otherwise
        #    its own lifecycle (exit) events get filtered out and positions never close.
        if STOCK_OPT_ENABLED:
            try:
                traders["stock_opt"] = PaperTrader(session, broker=broker, strategy="stock_opt",
                                                  capital=200000, allow_buy=True, allow_sell=False,
                                                  categories=["stock_option"])
            except Exception as e:
                log.error("Paper trader [stock_opt] init failed (%s); continuing without it.", e)

        # 5. equity_cash: equity stock buying (factor/momentum, deterministic, no LLM)
        if EQUITY_FACTOR_ENABLED:
            try:
                traders["equity_cash"] = PaperTrader(session, broker=broker, strategy="equity_cash",
                                                    capital=300000, allow_buy=True, allow_sell=False,
                                                    categories=["equity_cash"])
            except Exception as e:
                log.error("Paper trader [equity_cash] init failed (%s); continuing without it.", e)

    paper = traders.get(DEFAULT_STRATEGY)  # Backward-compat: `paper` = opt_buy

    # Deterministic strategy generators: OpeningScalp generates index option BUYS (routed to opt_buy);
    # the sellers/equity/stock-opt generate their own calls with strategy field set.
    scalp = OpeningScalp(session, paper) if (OPENING_SCALP_ENABLED and paper is not None) else None
    if scalp is not None:
        log.info("Opening gap scalp ENABLED — normal generation suppressed until %s", GEN_RESUME_TIME)

    sellers = {}
    if SELLING_ENABLED:
        # Two seller variants: spread-based and naked. Each routes to its own book + trader.
        if "opt_sell_spread" in traders:
            sellers["opt_sell_spread"] = OptionSeller(session, traders["opt_sell_spread"])
        # Naked is the same class, just configured differently. To run both, instantiate twice.
        # For now, keep it simple: just the spread variant runs (SELLING_STRUCTURE="spread" by default).

    equity_factor = EquityFactor(session, traders.get("equity_cash")) if EQUITY_FACTOR_ENABLED else None
    stock_opts = StockOptions(session, traders.get("stock_opt")) if STOCK_OPT_ENABLED else None

    opt_trigger = OptionGenTrigger()
    other_categories = [c for c in CATEGORIES if c != "index_option"]
    last_other_gen: datetime | None = None
    # Feed-health watchdog state (only evaluated inside the active polling window).
    last_feed_ok = time.monotonic()
    feed_alerted = False
    # Position-reconciliation state (LIVE only): throttle + de-dupe repeat alerts.
    last_reconcile = 0.0
    reconcile_sig = None
    briefing_date = None
    premarket_date = None
    gen_halt_date = None
    eod_close_date = None
    eod_date = None

    # Seed the weekly-reset marker from when the account was last (re)created, so a
    # mid-week restart doesn't wipe a week that already started fresh. (year, week).
    last_reset_week = None
    if PAPER_TRADING_ENABLED and PAPER_WEEKLY_RESET:
        acct = get_paper_account()
        if acct and acct.get("created_at"):
            last_reset_week = acct["created_at"].isocalendar()[:2]

    log.info("Advisory orchestrator started (poll=%ss, premarket=9:08, options=event-driven, others=%smin)",
             POLL_INTERVAL_SEC, OTHER_GEN_INTERVAL_MIN)

    # Clean the board before doing anything else: contracts that expired while we
    # were down, lapsed validity windows, and entries still waiting from a previous
    # day. The tracking pass also sweeps, but only during market hours — without
    # this, a night/pre-market restart leaves dead calls on the dashboard till 9:15.
    try:
        swept = await asyncio.to_thread(sweep_stale_calls)
        for evt in swept:
            await bot.push_call_event(evt)
        if swept:
            log.info("Startup sweep closed %s stale call(s)", len(swept))
    except Exception as e:
        log.error("Startup call sweep failed: %s", e)

    try:
        while True:
            now = datetime.now()
            today = now.date()

            # Liveness stamp for the Docker healthcheck — every iteration, so a hung
            # loop or dead process goes stale and the container is marked unhealthy.
            try:
                heartbeat.beat()
            except Exception as e:  # noqa: BLE001
                log.debug("Heartbeat write failed: %s", e)

            # Weekly fresh start — on the first loop of a new ISO week, wipe the
            # paper account back to PAPER_START_CAPITAL. Durable history (jsonl)
            # is untouched, so the Weekly/Daily P&L tables keep the full record.
            if PAPER_TRADING_ENABLED and PAPER_WEEKLY_RESET:
                cur_week = now.isocalendar()[:2]
                if cur_week != last_reset_week:
                    try:
                        await asyncio.to_thread(reset_paper_account, PAPER_START_CAPITAL)
                        log.info("Weekly fresh start — paper account reset to ₹%.0f (ISO week %s)",
                                 PAPER_START_CAPITAL, cur_week)
                        await bot.notify_owner(
                            f"🗓️ <b>New week — fresh start.</b>\n"
                            f"Paper account reset to ₹{PAPER_START_CAPITAL:,.0f}. "
                            f"Last week's results are saved in the Weekly P&L table.")
                    except Exception as e:  # noqa: BLE001
                        log.error("Weekly reset failed: %s", e)
                    last_reset_week = cur_week

            # Pre-market scan at 9:08 AM — generate calls based on opening levels.
            # Market opens at 9:15, so this gives 7 minutes to prepare for opening breakouts.
            # Pre-open heads-up only — NO generation here. Pre-open premiums are
            # stale (prev close), so calls made now rarely fill at the real open.
            # The first LIVE scan fires at 9:15 via the market-open trigger, and
            # 9:15–9:30 runs an opening-range-breakout strategy (see advisory_engine).
            if (_is_weekday(now) and now.time() >= _PREMARKET_T and premarket_date != today):
                try:
                    await bot.notify_owner(
                        "🔔 <b>Market opens in ~7 min.</b>\n"
                        "Watching the opening range — first LIVE setups fire right at "
                        "9:15, with opening-range-breakout focus through 9:30."
                    )
                    log.info("Pre-open heads-up sent")
                except Exception as e:  # noqa: BLE001
                    log.error("Pre-open heads-up failed: %s", e)
                premarket_date = today

            # Morning briefing — once per trading day.
            if (_is_weekday(now) and now.time() >= BRIEFING_AFTER
                    and now.time() <= _CLOSE_T and briefing_date != today):
                try:
                    await send_morning_briefing(bot)
                except Exception as e:
                    log.error("Morning briefing failed: %s", e)
                briefing_date = today

            if _in_market_hours(now):
                # 0a) Opening gap-fade scalp — deterministic, runs in the first minutes.
                if scalp is not None:
                    try:
                        for note in await asyncio.to_thread(scalp.step, now, price_lookup):
                            await bot.notify_owner(note)
                    except Exception as e:
                        log.error("Opening scalp step failed: %s", e)

                # 0b) Option-selling strategies — daily generation (spreads/naked).
                for seller in sellers.values():
                    try:
                        for note in await asyncio.to_thread(seller.step, now, price_lookup):
                            await bot.notify_owner(note)
                    except Exception as e:
                        log.error("Option seller step failed: %s", e)

                # 0c) Equity factor — weekly/monthly rebalance.
                if equity_factor is not None:
                    try:
                        for note in await asyncio.to_thread(equity_factor.step, now, price_lookup):
                            await bot.notify_owner(note)
                    except Exception as e:
                        log.error("Equity factor step failed: %s", e)

                # 0d) Stock options — daily generation (light, illiquid).
                if stock_opts is not None:
                    try:
                        for note in await asyncio.to_thread(stock_opts.step, now, price_lookup):
                            await bot.notify_owner(note)
                    except Exception as e:
                        log.error("Stock options step failed: %s", e)

                # 1) Lifecycle tracking every poll — the near-real-time entry/exit layer.
                try:
                    await run_tracking_pass(bot, price_lookup, traders)
                except Exception as e:
                    log.error("Tracking pass failed: %s", e)

                # 1b) Reconcile internal positions vs the broker's book (LIVE only).
                #     Read-only: alerts on any mismatch so a rejected/partial fill
                #     can't leave internal state silently out of sync. Paper has no
                #     broker book, so this is gated to live mode.
                if (RECONCILE_ENABLED and traders and EXECUTION_MODE == "live"
                        and time.time() - last_reconcile >= RECONCILE_INTERVAL_SEC):
                    last_reconcile = time.time()
                    try:
                        from core.reconcile import run_reconciliation
                        divergences = await asyncio.to_thread(run_reconciliation, broker)
                        if divergences:  # non-empty → mismatch
                            sig = tuple(sorted((d["code"], d["internal"], d["broker"])
                                               for d in divergences))
                            if sig != reconcile_sig:  # only alert when the picture changes
                                reconcile_sig = sig
                                detail = "\n".join(
                                    f"• {d['code']}: bot {d['internal']} vs broker {d['broker']} "
                                    f"(Δ{d['diff']:+d})" for d in divergences)
                                await bot.notify_owner(
                                    "🚨 <b>Position reconciliation MISMATCH</b>\n" + detail +
                                    "\n\nInternal state and the Dhan account disagree — verify "
                                    "before trusting the bot's positions or square-off.")
                                log.error("Reconciliation mismatch: %s", divergences)
                        elif divergences is not None and reconcile_sig is not None:
                            reconcile_sig = None  # back in sync — reset so a future drift re-alerts
                            log.info("Reconciliation clean again")
                    except Exception as e:
                        log.error("Reconciliation failed: %s", e)

                # Generation window: not before GEN_RESUME_AFTER (lets the opening
                # scalp own the first minutes) and not after GEN_HALT_AFTER (so
                # nothing fresh triggers near the close and carries overnight).
                if GEN_RESUME_AFTER <= now.time() < GEN_HALT_AFTER:
                    # 2) Event-driven index-option generation — fire when the market moves.
                    #    Skip when spots are unavailable (e.g. feed hiccup) so we don't
                    #    fire blindly or hammer the quote API without ATM grounding.
                    if session is not None:
                        try:
                            spots = await asyncio.to_thread(get_index_spots, session)
                            if spots:
                                last_feed_ok = time.monotonic()
                                if feed_alerted:
                                    await bot.notify_owner("✅ <b>Market-data feed recovered.</b>")
                                    feed_alerted = False
                                    log.info("Market-data feed recovered")
                                fire, reason, idxs = opt_trigger.check(spots)
                                if fire:
                                    log.info("Option scan triggered (%s)", reason or "—")
                                    await run_generation_cycle(bot, session, ["index_option"], indices=idxs)
                                    opt_trigger.commit(spots)
                        except Exception as e:
                            log.error("Option generation failed: %s", e)
                        # Feed-stale watchdog: a blind loop can't track stops or square
                        # off, so alert the owner ONCE if quotes dry up mid-session.
                        if FEED_ALERTS_ENABLED:
                            stale = time.monotonic() - last_feed_ok
                            if stale > FEED_STALE_SEC and not feed_alerted:
                                await bot.notify_owner(
                                    f"⚠️ <b>Market-data feed stale</b> — no index quotes for "
                                    f"~{stale:.0f}s. Tracking, stops and square-off may be blind. "
                                    f"Check the Dhan feed/token.")
                                feed_alerted = True
                                log.error("Market-data feed stale for %.0fs — owner alerted", stale)

                    # 3) Slower cadence for equity/futures/commodity.
                    if other_categories and (
                            last_other_gen is None
                            or (now - last_other_gen).total_seconds() >= OTHER_GEN_INTERVAL_MIN * 60):
                        try:
                            await run_generation_cycle(bot, session, other_categories)
                        except Exception as e:
                            log.error("Other-category generation failed: %s", e)
                        last_other_gen = now

            # Pre-close (15:28) — generation is already halted above; now cancel every
            # call still WAITING for entry so only in-trade calls remain for the 15:29
            # square-off. Nothing fresh can trigger and carry overnight.
            if (_is_weekday(now) and now.time() >= GEN_HALT_AFTER and gen_halt_date != today):
                try:
                    cancelled = await asyncio.to_thread(cancel_waiting_calls)
                    for evt in cancelled:
                        await bot.push_call_event(evt)
                    if cancelled:
                        log.info("Pre-close: cancelled %s waiting call(s); new generation halted", len(cancelled))
                except Exception as e:
                    log.error("Pre-close waiting-cancel failed: %s", e)
                gen_halt_date = today

            # EOD close — 1 min before the close, while the feed is still live.
            if (_is_weekday(now) and now.time() >= EOD_CLOSE_AFTER and eod_close_date != today):
                if EOD_SQUARE_OFF_ALL:
                    # Hard EOD rule: nothing carries overnight. Close EVERY open
                    # paper position at the day's last price, then force-close
                    # EVERY still-active call (in-trade booked, unfilled cancelled).
                    if paper is not None:
                        try:
                            notes = await asyncio.to_thread(paper.square_off_all, price_lookup)
                            for note in notes:
                                await bot.notify_owner(note)
                            if notes:
                                log.info("EOD square-off closed %s position(s)", len(notes))
                        except Exception as e:
                            log.error("EOD square-off failed: %s", e)
                    try:
                        closed = await asyncio.to_thread(force_close_all_calls, price_lookup)
                        for evt in closed:
                            await bot.push_call_event(evt)
                        if closed:
                            log.info("EOD square-off closed %s call(s)", len(closed))
                    except Exception as e:
                        log.error("EOD call square-off failed: %s", e)
                else:
                    # Settle paper positions whose options expire today (cash-settled at close).
                    if paper is not None:
                        try:
                            notes = await asyncio.to_thread(paper.settle_expiry, price_lookup, today)
                            for note in notes:
                                await bot.notify_owner(note)
                            if notes:
                                log.info("Expiry settlement closed %s position(s)", len(notes))
                        except Exception as e:
                            log.error("Expiry settlement failed: %s", e)
                    # EOD rule: no call carries overnight waiting for its trigger —
                    # cancel every unfilled entry; tomorrow's market gets fresh calls.
                    try:
                        swept = await asyncio.to_thread(sweep_stale_calls)
                        for evt in swept:
                            await bot.push_call_event(evt)
                        if swept:
                            log.info("EOD sweep closed %s stale call(s)", len(swept))
                    except Exception as e:
                        log.error("EOD call sweep failed: %s", e)
                eod_close_date = today

            # EOD digest — once per trading day, after the market has closed.
            if (_is_weekday(now) and now.time() >= EOD_AFTER and eod_date != today):
                try:
                    await send_eod_summary(bot)
                except Exception as e:
                    log.error("EOD summary failed: %s", e)
                eod_date = today

            await asyncio.sleep(POLL_INTERVAL_SEC)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(run())
