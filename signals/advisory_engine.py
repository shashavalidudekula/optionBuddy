"""
advisory_engine.py — AI research engine that generates trade-idea "calls".

This does NOT react to a user's existing positions. It scans the market and
produces fresh advisory calls with full trade parameters: entry, target(s),
stop-loss, and a research rationale.

Categories:
  - index_option : NIFTY / BANKNIFTY weekly option calls (strike, entry premium, T1/T2, SL)
  - equity       : Cash stock buy/sell calls (intraday/swing)
  - futures      : Index & stock futures directional calls
  - commodity    : MCX (gold, crude, silver) calls

Auto-publish gate: only calls with confidence >= MIN_CONFIDENCE are returned.
Every call is purely informational/advisory — no execution.
"""

import json
import re
from datetime import datetime

from config.settings import MIN_CONFIDENCE
from config.logger import get_logger
from core.llm import generate, LLMError, LLMQuotaError

log = get_logger("advisory_engine")

# Back-compat: older modules import GeminiQuotaError from here.
GeminiQuotaError = LLMQuotaError

# Required numeric fields per category for a call to be considered valid/tradeable.
_REQUIRED_FIELDS = ("instrument", "action", "entry_price", "target_1", "stop_loss")

# Strike + option type, tolerant of separators (matches "NIFTY 23400 PE" and
# "NIFTY-Jun2026-23400-PE"); used to normalise option instrument labels.
_OPTION_RE = re.compile(r"(\d{3,7})[\s\-]*(CE|PE)\b", re.IGNORECASE)

# Indices that must never be published as cash-equity calls (they're not tradeable
# in the cash segment — they belong to index_option / futures).
_INDEX_UNDERLYINGS = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "SENSEX", "BANKEX", "SENSEX50",
}

_CATEGORY_GUIDANCE = {
    "index_option": (
        "Focus on NIFTY, BANKNIFTY and SENSEX index options. Recommend a specific option "
        "(e.g. 'NIFTY 24500 CE', 'SENSEX 81000 PE'). All prices (entry/target/stop) are OPTION "
        "PREMIUMS in INR, "
        "not index points. Account for theta decay and IV. Prefer slightly OTM/ATM strikes "
        "with liquidity. Timeframe is usually 'intraday'.\n"
        "DIRECTION RULE (critical — match the option type to the intraday trend of the UNDERLYING):\n"
        "  • Confirmed UPTREND (intraday 'trend_5m' up, price above VWAP / above opening range, "
        "positive 'momentum_30m_pct') → BUY a CALL (CE), or SELL a PUT (PE).\n"
        "  • Confirmed DOWNTREND (trend down, below VWAP / below opening range, negative momentum) "
        "→ BUY a PUT (PE), or SELL a CALL (CE).\n"
        "  • NEVER buy a PUT (PE) while the index is trending UP, and NEVER buy a CALL (CE) while "
        "it is trending DOWN. Do not fade an active intraday trend unless you cite a SPECIFIC, "
        "named reversal signal (e.g. rejection at a stated resistance with momentum divergence) in "
        "the rationale. When the tape is genuinely flat/choppy, it is fine to return no option calls.\n"
        "IMPORTANT: When an 'option_chain' is provided in the market snapshot, you MUST pick a "
        "strike that exists in it and set 'entry_price' at (or very close to) that strike's live "
        "'premium'. Derive 'target_1'/'target_2'/'stop_loss' from that live premium so the call "
        "is realistic and trackable. Do not invent premiums that contradict the chain."
    ),
    "equity": (
        "Focus on liquid NSE cash stocks (large/mid cap), e.g. RELIANCE, HDFCBANK, TCS. Recommend "
        "BUY or SELL with a cash price entry, two targets and a stop-loss. Use technical structure "
        "(support/resistance, breakouts, moving averages) plus the news context. "
        "NEVER use an index (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, etc.) as an equity instrument — "
        "indices belong to the index_option or futures categories. Timeframe 'intraday' or 'swing'."
    ),
    "futures": (
        "Focus on index futures (NIFTY/BANKNIFTY FUT) and liquid stock futures. Give a directional "
        "BUY/SELL call with entry, targets and stop-loss in the futures price. Mind leverage and "
        "respect tight stop-losses. Timeframe 'intraday' or 'swing'."
    ),
    "commodity": (
        "Focus on MCX commodities: GOLD, SILVER, CRUDEOIL, NATURALGAS. Give a BUY/SELL call with "
        "entry, targets and stop-loss in the commodity's MCX price. Use global cues (DXY, Brent, "
        "geopolitics). Timeframe 'intraday' or 'positional'."
    ),
}


def _system_prompt(category: str) -> str:
    guidance = _CATEGORY_GUIDANCE.get(category, "")
    return (
        "You are a SEBI-style research analyst generating ACTIONABLE trade ideas for Indian "
        f"markets. Category: {category}.\n\n"
        f"{guidance}\n\n"
        "RULES:\n"
        '1. Respond ONLY with a valid JSON OBJECT of the form {"calls": [ ... ]}. '
        "No markdown, no prose outside the JSON.\n"
        "2. \"calls\" holds 0 to 3 of your HIGHEST-CONVICTION ideas. Quality over quantity. "
        'Use {"calls": []} if nothing is compelling right now.\n'
        "3. Each call object MUST have these keys:\n"
        '   "instrument" (e.g. "NIFTY 24500 CE" or "RELIANCE"),\n'
        '   "underlying" (e.g. "NIFTY", "RELIANCE", "GOLD"),\n'
        '   "action" ("BUY" or "SELL"),\n'
        '   "timeframe" ("intraday" | "swing" | "positional"),\n'
        '   "entry_price" (number), "entry_min" (number), "entry_max" (number),\n'
        '   "target_1" (number), "target_2" (number), "stop_loss" (number),\n'
        '   "confidence" (0-100 integer),\n'
        '   "rationale" (1-2 sentence research note explaining the setup).\n'
        "4. Risk-first: stop-loss must be realistic; reward:risk should be >= 1.5:1.\n"
        "5. Prices must be internally consistent: for BUY, target>entry>stop; "
        "for SELL, target<entry<stop.\n"
        "6. Be precise with numbers — these are published as advisory calls.\n"
        "7. GROUND every view in the data provided. The 'technicals' block is DAILY structure "
        "(RSI, EMA20/EMA50, 'trend', ATR14 for ~1-2x ATR stops, 20-day range). The 'intraday' "
        "block is the LIVE 5-minute read for TIMING: rsi14_5m, ema9_5m/ema21_5m and 'trend_5m', "
        "'vwap_state' (above/below VWAP), 'opening_range_state' and 'momentum_30m_pct'. Align the "
        "trade with intraday momentum and only fade it with a clear reason. Do NOT invent indicator "
        'values or cite TA you were not given. If nothing is high-conviction, use {"calls": []}.\n'
    )


def _build_market_prompt(market_data: dict, headlines: list[str]) -> str:
    mkt = json.dumps(market_data, indent=2, default=str)
    news = "\n".join(f"- {h}" for h in headlines) if headlines else "No fresh headlines."
    return (
        "## Live Market Snapshot\n" + mkt +
        "\n\n## Recent Headlines / Macro\n" + news +
        f"\n\n## Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n\n"
        'Generate your highest-conviction calls now. JSON object {"calls": [...]} only.'
    )


def _parse_calls(raw: str) -> list[dict]:
    """Extract the list of calls from the model response (JSON-mode or fenced)."""
    raw = (raw or "").strip()
    # In case a provider still wraps the JSON in a code fence, strip it.
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Failed to parse calls JSON: %s | raw: %s", e, raw[:300])
        return []
    if isinstance(data, dict):
        calls = data.get("calls")
        if isinstance(calls, list):
            return calls
        return [data]  # a single bare call object
    return data if isinstance(data, list) else []


def _is_valid(call: dict) -> bool:
    """Validate required fields exist and price relationships are coherent."""
    for f in _REQUIRED_FIELDS:
        if call.get(f) in (None, ""):
            log.debug("Call rejected (missing %s): %s", f, call.get("instrument"))
            return False
    try:
        entry = float(call["entry_price"])
        t1 = float(call["target_1"])
        sl = float(call["stop_loss"])
        action = str(call["action"]).upper()
    except (TypeError, ValueError):
        return False

    if action == "BUY" and not (t1 > entry > sl):
        log.debug("Call rejected (BUY price order): %s", call.get("instrument"))
        return False
    if action == "SELL" and not (t1 < entry < sl):
        log.debug("Call rejected (SELL price order): %s", call.get("instrument"))
        return False
    return True


def generate_calls(
    category: str,
    market_data: dict,
    headlines: list[str],
    exclude_instruments: set[str] | None = None,
) -> list[dict]:
    """Generate advisory calls for a category.

    Args:
        category: one of index_option | equity | futures | commodity
        market_data: live market snapshot dict
        headlines: recent news headlines
        exclude_instruments: instruments that already have an active call (skip duplicates)

    Returns:
        List of validated calls with confidence >= MIN_CONFIDENCE. Each dict has
        the `category` key set and is ready to pass to save_call().
    """
    exclude = exclude_instruments or set()
    system = _system_prompt(category)
    user = _build_market_prompt(market_data, headlines)

    try:
        text = generate(user, system=system, json_mode=True, max_tokens=1024, temperature=0.3)
        raw_calls = _parse_calls(text)
    except LLMQuotaError as e:
        log.warning("Call generation skipped for %s (quota): %s", category, e)
        return []
    except LLMError as e:
        log.error("Call generation failed for %s: %s", category, e)
        return []

    published: list[dict] = []
    for call in raw_calls:
        call["category"] = category

        # Normalise option labels to "<UNDERLYING> <STRIKE> <CE/PE>" so they display
        # cleanly and the tracker can resolve them (the model often echoes the raw
        # trading symbol like "NIFTY-Jun2026-23400-PE").
        if category == "index_option":
            m = _OPTION_RE.search(str(call.get("instrument", "")))
            undl = str(call.get("underlying", "")).upper().strip()
            if m and undl:
                call["instrument"] = f"{undl} {int(m.group(1))} {m.group(2).upper()}"

        # An index can't be a cash-equity trade; reject so we don't publish
        # untradeable "SELL NIFTY (equity)" ideas. Indices → options/futures.
        if category == "equity":
            undl = str(call.get("underlying", "")).upper().replace(" ", "")
            if undl in _INDEX_UNDERLYINGS:
                log.info("Rejecting index underlying in equity category: %s", call.get("instrument"))
                continue

        if not _is_valid(call):
            continue

        conf = int(call.get("confidence", 0) or 0)
        if conf < MIN_CONFIDENCE:
            log.info("Call below confidence gate (%s < %s): %s",
                     conf, MIN_CONFIDENCE, call.get("instrument"))
            continue

        if call.get("instrument") in exclude:
            log.debug("Skipping duplicate active instrument: %s", call.get("instrument"))
            continue

        published.append(call)

    # Layer 2 — tape gate: deterministically drop any call that fights a clear
    # intraday trend (e.g. a bearish PE while the index is rallying). Reacts to the
    # realized tape, never predicts. No-op unless TAPE_FILTER_ENABLED.
    try:
        from core.tape_filter import filter_calls as _tape_filter
        before = len(published)
        published = _tape_filter(published)
        if len(published) != before:
            log.info("Tape gate dropped %s of %s %s call(s)",
                     before - len(published), before, category)
    except Exception as e:  # noqa: BLE001
        log.error("Tape gate skipped (%s); publishing unfiltered.", e)

    log.info("Generated %s publishable %s calls (from %s candidates)",
             len(published), category, len(raw_calls))
    return published
