"""
telegram_advisory_bot.py — Advisory-only Telegram bot for research calls.

Delivers research calls and lifecycle updates to subscribers. There is NO order
execution and NO broker linking — this bot is purely informational/advisory.

Commands:
  /start       — subscribe to calls
  /stop        — pause receiving calls
  /calls       — all active calls, grouped + full detail (filter: /calls futures|equity|commodity|options)
  /track       — show track record (accuracy / win rate)
  /categories  — toggle which call categories you receive
  /pnl         — (owner only) live INDStocks positions & today's P&L
  /review      — (owner only) AI next-session plan for your open positions
  /paper       — (owner only) shadow/paper account performance (no real money)
  /help        — usage help

Push API (called by the main advisory loop):
  push_new_call(call, call_id)   — broadcast a fresh call to subscribers of that category
  push_call_event(event)         — broadcast a target/SL/expiry update
  broadcast(text)                — broadcast plain message to all active subscribers
"""

import asyncio
import html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PAPER_START_CAPITAL
from config.logger import get_logger
from data.advisory_store import (
    CATEGORIES,
    add_subscriber,
    set_subscriber_active,
    set_subscriber_categories,
    get_subscriber,
    get_subscribers_for_category,
    get_active_calls,
    get_recent_calls,
    get_track_record,
)

log = get_logger("advisory_bot")

DISCLAIMER = (
    "⚠️ _Advisory only — not investment advice. Markets carry risk; "
    "trade at your own discretion._"
)

# HTML variant for messages sent with parse_mode="HTML" (/pnl, /review).
DISCLAIMER_HTML = (
    "⚠️ <i>Advisory only — not investment advice. Markets carry risk; "
    "trade at your own discretion.</i>"
)

_TOKEN_EXPIRED_MSG = (
    "⚠️ <b>INDStocks token expired or invalid.</b>\n"
    "Refresh <code>INDSTOCKS_ACCESS_TOKEN</code> in <code>.env</code>, recreate the "
    "container (<code>docker compose up -d</code>), then try again.\n"
    "<i>Note: INDStocks tokens expire roughly daily (~07:00 IST).</i>"
)

CATEGORY_LABEL = {
    "index_option": "📊 Index Options",
    "equity": "📈 Equity",
    "futures": "🔮 Futures",
    "commodity": "🛢 Commodity",
}

EVENT_LABEL = {
    "entry_triggered": "📍 Entry triggered",
    "target1_hit": "🎯 Target 1 hit",
    "target_hit": "✅ Target hit — call closed",
    "sl_hit": "🛑 Stop-loss hit — call closed",
    "expired": "⌛ Call expired",
    "invalidated": "🔄 Trend reversed — call cut early",
}


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def format_call(call: dict, call_id: int) -> str:
    """Format a fresh call for Telegram (Markdown)."""
    action = str(call.get("action", "")).upper()
    arrow = "🟢" if action == "BUY" else "🔴"
    cat = CATEGORY_LABEL.get(call.get("category", ""), call.get("category", ""))
    tf = str(call.get("timeframe", "")).upper()
    conf = call.get("confidence", 0)

    emin, emax = call.get("entry_min"), call.get("entry_max")
    if emin is not None and emax is not None:
        entry_line = f"Entry: {_fmt_price(emin)} – {_fmt_price(emax)}"
    else:
        entry_line = f"Entry: {_fmt_price(call.get('entry_price'))}"

    lines = [
        f"{arrow} *{action}* — `{call.get('instrument', '?')}`  [{cat} · {tf}]",
        f"Confidence: *{conf}%*",
        "",
        entry_line,
        f"🎯 Target 1: {_fmt_price(call.get('target_1'))}",
    ]
    if call.get("target_2") is not None:
        lines.append(f"🎯 Target 2: {_fmt_price(call.get('target_2'))}")
    lines.append(f"🛑 Stop-loss: {_fmt_price(call.get('stop_loss'))}")

    if call.get("rationale"):
        lines += ["", f"📋 {call['rationale']}"]

    lines += ["", f"_Call #{call_id}_", DISCLAIMER]
    return "\n".join(lines)


def format_event(event: dict) -> str:
    """Format a lifecycle update for Telegram (Markdown)."""
    label = EVENT_LABEL.get(event["event_type"], event["event_type"])
    inst = event["instrument"]
    parts = [f"{label}\n`{inst}`  (Call #{event['call_id']})"]

    if event.get("result_pct") is not None:
        pct = event["result_pct"]
        sign = "🟢 +" if pct >= 0 else "🔴 "
        parts.append(f"Result: {sign}{pct:.2f}%")
    if event.get("price") is not None:
        parts.append(f"At: {_fmt_price(event['price'])}")
    return "\n".join(parts)


# ── /pnl & /review — live portfolio view (read-only, owner only) ───────────────

class IndStocksAuthError(Exception):
    """Raised when the INDStocks token is expired/invalid (HTTP 401)."""


def _first(d: dict, *keys, default=None):
    """Return the first present, non-empty value among keys (API field aliases)."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch_open_closed() -> tuple[list[dict], list[dict]]:
    """Fetch live broker positions (active provider) → (open, closed) dicts.

    Read-only portfolio view — NO execution. Builds a fresh session each call so
    it always uses the current token (broker tokens expire ~daily). Field names
    are alias-tolerant so both INDStocks (snake_case) and Dhan (camelCase) parse.
    Raises IndStocksAuthError on an expired/invalid token; other failures
    propagate to the caller.
    """
    import requests
    from core.market_data_provider import get_session, get_positions

    try:
        positions = get_positions(get_session())
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 401:
            raise IndStocksAuthError() from e
        raise
    except ValueError as e:  # provider get_session() rejected an invalid/expired token
        raise IndStocksAuthError() from e

    open_pos: list[dict] = []
    closed_pos: list[dict] = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        qty = int(_num(_first(p, "net_qty", "net_quantity", "netQty", default=0)))
        sym = _first(p, "symbol", "trading_symbol", "custom_symbol",
                     "tradingSymbol", "customSymbol", default="?")
        strike = _first(p, "drv_strike_price", "strike_price", "drvStrikePrice")
        opt = _first(p, "drv_option_type", "option_type", "drvOptionType")
        label = f"{sym}{strike}{opt}" if strike and opt else sym

        if qty != 0:
            upnl_raw = _first(p, "pnl_absolute", "unrealized_profit", "unrealised", "unrealizedProfit")
            open_pos.append({
                "label": label,
                "direction": "LONG" if qty > 0 else "SHORT",
                "qty": abs(qty),
                "avg_price": _num(_first(p, "avg_price", "buy_avg", "net_avg_price", "buyAvg", "costPrice")),
                "ltp": _num(_first(p, "ltp", "last_price", "live_price", "last_traded_price", "lastTradedPrice")),
                "unrealised": _num(upnl_raw) if upnl_raw is not None else None,
            })
        else:
            closed_pos.append({
                "label": label,
                "buy_avg": _num(_first(p, "buy_avg", "buyAvg")),
                "sell_avg": _num(_first(p, "sell_avg", "sellAvg")),
                "realised": _num(_first(p, "realized_profit", "realised_profit", "realized", "realizedProfit")),
            })
    return open_pos, closed_pos


def build_pnl_report() -> str:
    """Render the owner's live positions & today's P&L as an HTML table."""
    try:
        open_pos, closed_pos = _fetch_open_closed()
    except IndStocksAuthError:
        return _TOKEN_EXPIRED_MSG
    except Exception as e:  # noqa: BLE001
        log.error("PnL fetch failed: %s", e)
        return f"⚠️ <b>Couldn't fetch positions:</b> {e}"

    if not open_pos and not closed_pos:
        return "ℹ️ <b>No open positions or trades today.</b>"

    lines: list[str] = []
    if open_pos:
        lines.append("<b>⏳ OPEN POSITIONS</b>")
        lines.append("─" * 28)
        unreal_total = 0.0
        has_unreal = False
        for p in open_pos:
            emoji = "📈" if p["direction"] == "LONG" else "📉"
            lines.append(f"{emoji} <b>{p['label']}</b>  {p['direction']} ×{p['qty']}")
            sub = f"   Avg ₹{p['avg_price']:,.2f}"
            if p["ltp"]:
                sub += f" | LTP ₹{p['ltp']:,.2f}"
            if p["unrealised"] is not None:
                unreal_total += p["unrealised"]
                has_unreal = True
                sub += f" | {'🟢' if p['unrealised'] >= 0 else '🔴'} ₹{p['unrealised']:,.0f}"
            lines.append(sub)
        if has_unreal:
            lines.append(f"<b>Unrealised: {'🟢' if unreal_total >= 0 else '🔴'} ₹{unreal_total:,.2f}</b>")
        lines.append("")

    if closed_pos:
        lines.append("<b>📊 TODAY'S CLOSED TRADES</b>")
        lines.append("─" * 28)
        realized_total = 0.0
        for p in closed_pos:
            realized_total += p["realised"]
            lines.append(f"{p['label']}  →  ₹{p['realised']:,.0f} {'✅' if p['realised'] >= 0 else '❌'}")
            lines.append(f"   Buy ₹{p['buy_avg']:,.2f} | Sell ₹{p['sell_avg']:,.2f}")
        lines.append(f"<b>Realised P&amp;L: {'🟢' if realized_total >= 0 else '🔴'} ₹{realized_total:,.2f}</b>")

    return "\n".join(lines).rstrip()


# AI action → emoji for the /review next-session plan.
_ACTION_EMOJI = {
    "HOLD": "✊",
    "EXIT": "🚪",
    "BOOK_PARTIAL": "💰",
    "ADD": "➕",
    "HEDGE": "🛡",
}


def build_position_review() -> str:
    """Fetch open positions + market context, ask Gemini for a next-session plan."""
    import html

    from core.market_data_provider import get_session, get_market_snapshot
    from data.news_fetcher import get_top_headlines
    from core.llm import LLMQuotaError
    from signals.position_advisor import review_positions

    try:
        open_pos, _ = _fetch_open_closed()
    except IndStocksAuthError:
        return _TOKEN_EXPIRED_MSG
    except Exception as e:  # noqa: BLE001
        log.error("Review fetch failed: %s", e)
        return f"⚠️ <b>Couldn't fetch positions:</b> {html.escape(str(e))}"

    if not open_pos:
        return "ℹ️ <b>No open positions to review.</b>"

    # Market context is best-effort — the review still works on positions alone.
    try:
        market = get_market_snapshot(get_session())
    except Exception as e:  # noqa: BLE001
        log.warning("Review market snapshot failed: %s", e)
        market = {}
    try:
        headlines = get_top_headlines()
    except Exception as e:  # noqa: BLE001
        log.warning("Review headlines failed: %s", e)
        headlines = []

    try:
        review = review_positions(open_pos, market, headlines)
    except LLMQuotaError:
        return (
            "🤖 <b>AI review unavailable — model quota/rate limit reached.</b>\n"
            "Please try again in a little while."
        )
    except Exception as e:  # noqa: BLE001
        log.error("Position review failed: %s", e)
        return f"⚠️ <b>AI review failed:</b> {html.escape(str(e))}"

    items = review.get("positions", [])
    if not items:
        return "🤖 <b>AI couldn't form a clear view right now.</b> Try again shortly."

    lines = ["🤖 <b>NEXT-SESSION PLAN</b>", "─" * 28]
    for it in items:
        label = html.escape(str(it.get("instrument", "?")))
        action = str(it.get("action", "HOLD")).upper()
        emoji = _ACTION_EMOJI.get(action, "•")
        head = f"{emoji} <b>{label}</b> — {action.replace('_', ' ')}"
        conf = it.get("confidence")
        if conf not in (None, ""):
            head += f"  ({_num(conf):.0f}%)"
        lines.append(head)

        levels = []
        if it.get("target") not in (None, ""):
            levels.append(f"🎯 ₹{_num(it['target']):,.2f}")
        if it.get("stop_loss") not in (None, ""):
            levels.append(f"🛑 ₹{_num(it['stop_loss']):,.2f}")
        if levels:
            lines.append("   " + "  ".join(levels))

        reason = it.get("reason")
        if reason:
            lines.append(f"   <i>{html.escape(str(reason))}</i>")

    overall = review.get("overall")
    if overall:
        lines += ["", f"<b>Overall:</b> {html.escape(str(overall))}"]
    lines += ["", DISCLAIMER_HTML]
    return "\n".join(lines)


# ── /paper — simulated (shadow) account performance (owner only) ───────────────

def _paper_trade_table(rows: list[dict]) -> str:
    """Build a monospaced, column-aligned ledger of closed paper trades.

    Telegram bot messages can't render true text colours, so profit/loss is
    flagged with a trailing 🟢/🔴 (kept last so column alignment is preserved)
    and every P&L carries an explicit +/− sign.
    """
    header = f"{'Date':<5} {'Instrument':<15} {'Side':<4} {'Entry':>8} {'Exit':>8} {'P&L':>9}"
    out = [header]
    for p in rows:
        closed_at = p.get("closed_at")
        d = closed_at.strftime("%m/%d") if closed_at else "  -  "
        instr = str(p["instrument"])[:15]
        side = str(p["action"]).upper()[:4]
        entry = float(p["entry_price"])
        exit_p = float(p["last_price"]) if p["last_price"] is not None else entry
        pnl = float(p["realized_pnl"])
        emoji = "🟢" if pnl >= 0 else "🔴"
        out.append(
            f"{d:<5} {instr:<15} {side:<4} {entry:>8.2f} {exit_p:>8.2f} "
            f"{pnl:>+9,.0f}  {emoji}"
        )
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def build_paper_report() -> str:
    """Render the paper (shadow) account: equity, returns, P&L, win rate, positions."""
    from data.advisory_store import (
        get_paper_stats,
        get_open_paper_positions,
        get_closed_paper_positions,
        get_call_levels,
    )

    s = get_paper_stats()
    if not s:
        return "ℹ️ <b>Paper account not initialised yet.</b> It starts once the agent runs."

    total_emoji = "🟢" if s["total_return_pct"] >= 0 else "🔴"
    today_emoji = "🟢" if s["today_realized"] >= 0 else "🔴"
    lines = [
        "🧪 <b>PAPER ACCOUNT</b> <i>(shadow — no real money)</i>",
        "─" * 28,
        f"Equity: <b>₹{s['equity']:,.0f}</b>  {total_emoji} {s['total_return_pct']:+.2f}%",
        f"Start: ₹{s['starting_capital']:,.0f}  |  Cash: ₹{s['cash']:,.0f}",
        f"Realised: ₹{s['realized_pnl']:,.0f}  |  Unrealised: ₹{s['unrealized_pnl']:,.0f}",
        f"Today: {today_emoji} ₹{s['today_realized']:,.0f}",
        "",
        f"Closed trades: <b>{s['closed_trades']}</b>  "
        f"(W {s['wins']} / L {s['losses']}, win rate <b>{s['win_rate']}%</b>)",
        f"Max drawdown: {s['max_drawdown_pct']:.2f}%",
    ]

    open_pos = get_open_paper_positions()
    if open_pos:
        levels = get_call_levels([p.get("call_id") for p in open_pos])
        lines += ["", f"<b>⏳ OPEN ({len(open_pos)})</b>", "─" * 28]
        for p in open_pos:
            last = float(p["last_price"]) if p["last_price"] is not None else float(p["entry_price"])
            entry = float(p["entry_price"])
            sign = 1 if str(p["action"]).upper() == "BUY" else -1
            upnl = sign * int(p["remaining_qty"]) * (last - entry)
            ue = "🟢" if upnl >= 0 else "🔴"
            lines.append(f"{p['action']} <b>{p['instrument']}</b> ×{p['remaining_qty']}")
            lines.append(f"   Entry ₹{entry:,.2f} | LTP ₹{last:,.2f} | {ue} ₹{upnl:,.0f}")
            lv = levels.get(p.get("call_id")) or {}
            tgt = " / ".join(
                f"₹{float(t):,.2f}" for t in (lv.get("target_1"), lv.get("target_2")) if t is not None
            )
            plan = []
            if tgt:
                plan.append(f"🎯 {tgt}")
            if lv.get("stop_loss") is not None:
                plan.append(f"🛑 ₹{float(lv['stop_loss']):,.2f}")
            if plan:
                lines.append("   " + "  ".join(plan))

    closed = get_closed_paper_positions(15)
    if closed:
        head = f"<b>📒 CLOSED TRADES ({s['closed_trades']})</b>"
        if s["closed_trades"] > len(closed):
            head += f" <i>— showing last {len(closed)}</i>"
        lines += ["", head, _paper_trade_table(closed),
                  "<i>🟢 profit · 🔴 loss · amounts in ₹</i>"]

    lines += ["", DISCLAIMER_HTML]
    return "\n".join(lines)


def _parse_date_arg(s: str) -> str | None:
    """Parse a user date (YYYY/MM/DD, YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY) → YYYY-MM-DD."""
    import datetime as _dt
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _paper_day_table(trades: list[dict]) -> str:
    header = f"{'Time':<5} {'Instrument':<15} {'Side':<4} {'Entry':>8} {'Exit':>8} {'P&L':>9}"
    out = [header]
    for t in trades:
        tm = str(t.get("ts", ""))[11:16] or "  -  "
        instr = str(t.get("instrument", ""))[:15]
        side = str(t.get("action", "")).upper()[:4]
        entry = float(t.get("entry") or 0)
        exit_p = float(t.get("exit") or 0)
        pnl = float(t.get("pnl") or 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        out.append(f"{tm:<5} {instr:<15} {side:<4} {entry:>8.2f} {exit_p:>8.2f} {pnl:>+9,.0f}  {emoji}")
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def build_paper_day_report(date_str: str) -> str:
    """Render the paper results for one date.

    Reads the database first (authoritative for the current session, before any
    overnight reset), and falls back to the durable file log for older dates the
    DB no longer holds.
    """
    from data.advisory_store import get_paper_fills_on
    from core.paper_trader import read_paper_day

    trades: list[dict] = []
    for fr in get_paper_fills_on(date_str):  # one row per realized booking (partial + close)
        ts = fr.get("ts")
        trades.append({
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts or ""),
            "instrument": fr.get("instrument"),
            "action": fr.get("action") or "",
            "entry": float(fr["entry_price"]) if fr.get("entry_price") is not None else 0.0,
            "exit": float(fr["price"]) if fr.get("price") is not None else 0.0,
            "pnl": float(fr.get("realized_pnl") or 0),
        })
    if not trades:  # fall back to the durable archive (past days after a reset)
        trades = read_paper_day(date_str)["trades"]

    if not trades:
        return (f"ℹ️ <b>No paper trades recorded on {date_str}.</b>\n"
                "<i>Daily history is captured from when this build went live; earlier days "
                "may be unavailable.</i>")

    pnl = round(sum(float(t.get("pnl", 0) or 0) for t in trades), 2)
    wins = sum(1 for t in trades if float(t.get("pnl", 0) or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0) or 0) < 0)
    ret = round(pnl / PAPER_START_CAPITAL * 100, 2) if PAPER_START_CAPITAL else 0.0
    emoji = "🟢" if pnl >= 0 else "🔴"
    lines = [
        f"🧪 <b>PAPER — {date_str}</b> <i>(shadow — no real money)</i>",
        "─" * 28,
        f"Realised P&L: {emoji} ₹{pnl:,.0f}  ({ret:+.2f}% on ₹{PAPER_START_CAPITAL:,.0f})",
        f"Trades: <b>{len(trades)}</b>  (W {wins} / L {losses})",
        "",
        _paper_day_table(trades),
        "<i>🟢 profit · 🔴 loss · amounts in ₹</i>",
        "",
        DISCLAIMER_HTML,
    ]
    return "\n".join(lines)


# ── /calls — rich, grouped view of every active call (all categories) ──────────

_CALL_STATUS_LABEL = {
    "active": "🕒 waiting for entry",
    "entry_triggered": "📍 in trade (entry hit)",
    "target1_hit": "🎯 T1 booked, trailing",
}

# Friendly category arguments → canonical category.
_CAT_ARG = {
    "options": "index_option", "option": "index_option", "index": "index_option",
    "index_option": "index_option", "nifty": "index_option",
    "futures": "futures", "fut": "futures", "future": "futures",
    "equity": "equity", "equities": "equity", "stocks": "equity", "stock": "equity",
    "commodity": "commodity", "commodities": "commodity", "mcx": "commodity",
}

_CATEGORY_ORDER = ["index_option", "equity", "futures", "commodity"]


def _fmt_expiry(v) -> str | None:
    """Format an option_expiry (date or 'YYYY-MM-DD' str) as 'DD Mon'."""
    if not v:
        return None
    try:
        return v.strftime("%d %b")
    except AttributeError:
        s = str(v)[:10]
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, "%Y-%m-%d").strftime("%d %b")
        except ValueError:
            return s


def _call_block_html(c: dict, live) -> str:
    action = str(c.get("action", "")).upper()
    arrow = "🟢" if action == "BUY" else "🔴"
    status = _CALL_STATUS_LABEL.get(str(c.get("status", "")), str(c.get("status", "")))

    emin, emax = c.get("entry_min"), c.get("entry_max")
    if emin not in (None, "") and emax not in (None, ""):
        entry = f"₹{float(emin):,.2f} – ₹{float(emax):,.2f}"
    else:
        entry = f"₹{float(c.get('entry_price') or 0):,.2f}"

    head = (f"{arrow} <b>{action} {html.escape(str(c.get('instrument', '?')))}</b>  ·  "
            f"conf {c.get('confidence', 0)}%  ·  #{c.get('id')}")
    exp = _fmt_expiry(c.get("option_expiry"))
    if exp:
        head += f"  ·  ⏳ exp {exp}"
    parts = [
        head,
        f"   {status}" + (f"  ·  LTP ₹{float(live):,.2f}" if live is not None else ""),
        f"   Entry: {entry}",
    ]
    tg = " / ".join(f"₹{float(t):,.2f}" for t in (c.get("target_1"), c.get("target_2")) if t is not None)
    if tg:
        parts.append(f"   🎯 {tg}")
    if c.get("stop_loss") is not None:
        parts.append(f"   🛑 ₹{float(c['stop_loss']):,.2f}")
    return "\n".join(parts)


def build_calls_report(category: str | None = None) -> str:
    """Rich, grouped view of active calls (optionally one category), with live LTP."""
    from data.advisory_store import get_active_calls

    calls = get_active_calls()
    if category:
        calls = [c for c in calls if c.get("category") == category]
    if not calls:
        scope = CATEGORY_LABEL.get(category, "").strip() if category else "active"
        return f"ℹ️ <b>No {scope or 'active'} calls right now.</b> You'll be alerted on the next setup."

    # Best-effort live prices (degrade silently if the data feed is unavailable).
    price_of: dict = {}
    try:
        from core.market_data_provider import get_session, make_price_lookup, warm_instruments
        session = get_session()
        warm_instruments(session)  # ensure scrip resolution works even before first gen
        lookup = make_price_lookup(session)
        for c in calls:
            try:
                price_of[c.get("id")] = lookup(c)
            except Exception:  # noqa: BLE001
                price_of[c.get("id")] = None
    except Exception:  # noqa: BLE001
        pass

    title = "📋 <b>ACTIVE CALLS</b>"
    if category:
        title = f"📋 <b>ACTIVE CALLS — {CATEGORY_LABEL.get(category, category)}</b>"
    lines = [title, "─" * 28]
    for cat in _CATEGORY_ORDER:
        group = [c for c in calls if c.get("category") == cat]
        if not group:
            continue
        lines += ["", f"<b>{CATEGORY_LABEL.get(cat, cat)} ({len(group)})</b>"]
        for c in group[:12]:
            lines.append(_call_block_html(c, price_of.get(c.get("id"))))
    lines += ["", DISCLAIMER_HTML]
    return "\n".join(lines)


class TelegramAdvisoryBot:
    """Advisory-only Telegram bot with subscriber management and call push."""

    def __init__(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("calls", self._cmd_calls))
        self.app.add_handler(CommandHandler("track", self._cmd_track))
        self.app.add_handler(CommandHandler("categories", self._cmd_categories))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("review", self._cmd_review))
        self.app.add_handler(CommandHandler("paper", self._cmd_paper))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CallbackQueryHandler(self._on_category_toggle, pattern=r"^cat:"))

    # ── Commands ────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        username = update.effective_user.username or update.effective_user.first_name or ""
        add_subscriber(chat_id, username)
        await update.message.reply_text(
            "👋 *Welcome to OptionBuddy Advisory!*\n\n"
            "You'll now receive research-backed trade calls with entry, target & stop-loss "
            "for Index Options, Equity, Futures and Commodity.\n\n"
            "• /calls — active calls\n"
            "• /track — our track record\n"
            "• /categories — choose what you receive\n"
            "• /stop — pause calls\n\n"
            + DISCLAIMER,
            parse_mode="Markdown",
        )
        log.info("New subscriber via /start: %s (%s)", chat_id, username)

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        set_subscriber_active(update.effective_chat.id, False)
        await update.message.reply_text(
            "⏸ You've paused calls. Send /start anytime to resume."
        )

    async def _cmd_calls(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """All active calls, grouped by category with full details + live price.

        Optional filter: /calls futures | equity | commodity | options
        """
        arg = (ctx.args[0].lower() if getattr(ctx, "args", None) else None)
        category = _CAT_ARG.get(arg) if arg else None
        if arg and category is None:
            await update.message.reply_text(
                "Filter not recognised. Try: <code>/calls</code>, <code>/calls futures</code>, "
                "<code>/calls equity</code>, <code>/calls commodity</code> or "
                "<code>/calls options</code>.",
                parse_mode="HTML",
            )
            return
        report = await asyncio.to_thread(build_calls_report, category)
        await update.message.reply_text(report, parse_mode="HTML")

    async def _cmd_track(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rec = get_track_record(days=30)
        o = rec["overall"]
        msg = [
            "📊 *Track Record (last 30 days)*",
            "",
            f"Total calls closed: *{o['total']}*",
            f"Wins: *{o['wins']}*  |  Losses: *{o['losses']}*",
            f"Win rate: *{o['win_rate']}%*",
            f"Avg return / call: *{o['avg_return']}%*",
            "",
            "*By category:*",
        ]
        for cat, s in rec["by_category"].items():
            if s["total"]:
                msg.append(
                    f"{CATEGORY_LABEL.get(cat, cat)}: {s['win_rate']}% "
                    f"({s['wins']}/{s['total']}), avg {s['avg_return']}%"
                )
        msg += ["", DISCLAIMER]
        await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

    async def _cmd_categories(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        sub = get_subscriber(update.effective_chat.id)
        if not sub:
            add_subscriber(update.effective_chat.id,
                            update.effective_user.username or "")
            sub = get_subscriber(update.effective_chat.id)
        await update.message.reply_text(
            "Tap to toggle the categories you want to receive:",
            reply_markup=self._category_keyboard(sub.get("categories", "")),
        )

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the owner's live INDStocks positions & today's P&L (read-only)."""
        if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("🔒 /pnl is restricted to the account owner.")
            return
        await update.message.reply_text("📊 Fetching your positions…")
        report = await asyncio.to_thread(build_pnl_report)
        await update.message.reply_text(report, parse_mode="HTML")

    async def _cmd_review(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """AI next-session action plan over the owner's open positions (read-only)."""
        if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("🔒 /review is restricted to the account owner.")
            return
        await update.message.reply_text(
            "🤖 Analysing your open positions… this can take a few seconds."
        )
        report = await asyncio.to_thread(build_position_review)
        await update.message.reply_text(report, parse_mode="HTML")

    async def _cmd_paper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Shadow account: /paper = live; /paper YYYY/MM/DD = that date's results (owner only)."""
        if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("🔒 /paper is restricted to the account owner.")
            return
        arg = (ctx.args[0] if getattr(ctx, "args", None) else "").strip()
        if arg:
            ds = _parse_date_arg(arg)
            if not ds:
                await update.message.reply_text(
                    "Use <code>/paper YYYY/MM/DD</code> — e.g. <code>/paper 2026/06/02</code>.",
                    parse_mode="HTML",
                )
                return
            report = await asyncio.to_thread(build_paper_day_report, ds)
        else:
            report = await asyncio.to_thread(build_paper_report)
        await update.message.reply_text(report, parse_mode="HTML")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the active LLM / data feed / execution mode + scan cadence (owner only)."""
        if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("🔒 /status is restricted to the account owner.")
            return
        from config.settings import (
            LLM_PROVIDER, GEMINI_MODEL, OPENAI_MODEL, AZURE_OPENAI_DEPLOYMENT,
            MARKET_DATA_PROVIDER, EXECUTION_MODE, DHAN_ALLOW_LIVE_ORDERS,
            POLL_INTERVAL_SEC, OTHER_GEN_INTERVAL_MIN, PAPER_TRADING_ENABLED,
            PAPER_RISK_PCT, PAPER_MAX_OPEN, PAPER_PARTIAL_FRACTION, MIN_CONFIDENCE,
        )
        model = {"gemini": GEMINI_MODEL, "openai": OPENAI_MODEL,
                 "azure": AZURE_OPENAI_DEPLOYMENT}.get(LLM_PROVIDER, "—")
        if EXECUTION_MODE == "live":
            exec_line = "live · ORDERS ON 🔴" if DHAN_ALLOW_LIVE_ORDERS else "live · dry-run (orders guarded)"
        else:
            exec_line = "paper (no real money)"
        sell = int(PAPER_PARTIAL_FRACTION * 100)
        lines = [
            "🩺 <b>OptionBuddy status</b>",
            f"• 🧠 LLM: <b>{LLM_PROVIDER}</b> — <code>{model}</code>",
            f"• 📡 Market data: <b>{MARKET_DATA_PROVIDER}</b>",
            f"• ⚙️ Execution: <b>{exec_line}</b>",
            f"• 📝 Paper: {'on' if PAPER_TRADING_ENABLED else 'off'} · risk {PAPER_RISK_PCT * 100:.0f}% "
            f"· max {PAPER_MAX_OPEN} open · min conf {MIN_CONFIDENCE}%",
            f"• 🎯 T1 rule: book {sell}% / hold {100 - sell}% → SL to breakeven",
            "• ⏱ Options scan: 09:15–09:45 ~1.5s · 09:45–10:30 20s · 10:30–12:00 45s "
            "· 12:00–13:00 30s · 13:00–15:30 10s (+ instant on a 0.25% reversal)",
            f"• ⏱ Tracking every {POLL_INTERVAL_SEC}s · equity/futures/commodity every {OTHER_GEN_INTERVAL_MIN}min",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "*OptionBuddy Advisory — Help*\n\n"
            "We send research-backed trade calls with clear entry, target & stop-loss, "
            "then alert you when a target or stop-loss is hit.\n\n"
            "• /calls — all active calls (full detail + live price)\n"
            "    ↳ filter: /calls futures · /calls equity · /calls commodity · /calls options\n"
            "• /track — accuracy & win rate\n"
            "• /categories — choose Index Options / Equity / Futures / Commodity\n"
            "• /pnl — your live positions & today's P&L (owner only)\n"
            "• /review — AI plan for your open positions next session (owner only)\n"
            "• /paper — shadow account performance, no real money (owner only)\n"
            "    ↳ /paper YYYY/MM/DD — that date's results\n"
            "• /status — active LLM, data feed & mode + scan cadence (owner only)\n"
            "• /stop — pause  •  /start — resume\n\n"
            + DISCLAIMER,
            parse_mode="Markdown",
        )

    # ── Category toggle keyboard ──────────────────────────────────────────────

    def _category_keyboard(self, current_csv: str) -> InlineKeyboardMarkup:
        current = set(c for c in (current_csv or "").split(",") if c)
        rows = []
        for cat in CATEGORIES:
            on = cat in current
            mark = "✅" if on else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} {CATEGORY_LABEL.get(cat, cat)}",
                callback_data=f"cat:{cat}",
            )])
        return InlineKeyboardMarkup(rows)

    async def _on_category_toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat.id
        cat = query.data.split(":", 1)[1]

        sub = get_subscriber(chat_id)
        current = set(c for c in (sub.get("categories", "") if sub else "").split(",") if c)
        if cat in current:
            current.discard(cat)
        else:
            current.add(cat)
        # Preserve canonical ordering
        ordered = [c for c in CATEGORIES if c in current]
        set_subscriber_categories(chat_id, ordered)

        await query.edit_message_reply_markup(
            reply_markup=self._category_keyboard(",".join(ordered))
        )

    # ── Push API ──────────────────────────────────────────────────────────────

    async def push_new_call(self, call: dict, call_id: int) -> None:
        """Broadcast a fresh call to all subscribers of its category."""
        text = format_call(call, call_id)
        recipients = get_subscribers_for_category(call.get("category", ""))
        sent = 0
        for sub in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode="Markdown"
                )
                sent += 1
            except Exception as e:
                log.warning("Failed to push call to %s: %s", sub["chat_id"], e)
        log.info("Call #%s pushed to %s subscribers (%s)", call_id, sent, call.get("category"))

    async def push_call_event(self, event: dict) -> None:
        """Broadcast a lifecycle update (target/SL/expiry) to subscribers of the category."""
        text = format_event(event)
        recipients = get_subscribers_for_category(event.get("category", ""))
        for sub in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode="Markdown"
                )
            except Exception as e:
                log.warning("Failed to push event to %s: %s", sub["chat_id"], e)

    async def broadcast(self, text: str, parse_mode: str = "Markdown") -> None:
        """Broadcast a plain message to all active subscribers."""
        from data.advisory_store import get_all_active_subscribers
        for sub in get_all_active_subscribers():
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode=parse_mode
                )
            except Exception as e:
                log.warning("Failed to broadcast to %s: %s", sub["chat_id"], e)

    async def notify_owner(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a private message to the account owner only (e.g. paper-trade fills)."""
        if not TELEGRAM_CHAT_ID:
            return
        try:
            await self.app.bot.send_message(
                chat_id=int(TELEGRAM_CHAT_ID), text=text, parse_mode=parse_mode
            )
        except Exception as e:
            log.warning("Failed to notify owner: %s", e)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Advisory Telegram bot polling started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Advisory Telegram bot stopped")
