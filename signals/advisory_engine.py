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

from google import genai

from config.settings import GEMINI_API_KEY, GEMINI_MODEL, MIN_CONFIDENCE
from config.logger import get_logger

log = get_logger("advisory_engine")

_client = genai.Client(api_key=GEMINI_API_KEY)

# Required numeric fields per category for a call to be considered valid/tradeable.
_REQUIRED_FIELDS = ("instrument", "action", "entry_price", "target_1", "stop_loss")

_CATEGORY_GUIDANCE = {
    "index_option": (
        "Focus on NIFTY and BANKNIFTY weekly options. Recommend a specific option "
        "(e.g. 'NIFTY 24500 CE'). All prices (entry/target/stop) are OPTION PREMIUMS in INR, "
        "not index points. Account for theta decay and IV. Prefer slightly OTM/ATM strikes "
        "with liquidity. Timeframe is usually 'intraday'.\n"
        "IMPORTANT: When an 'option_chain' is provided in the market snapshot, you MUST pick a "
        "strike that exists in it and set 'entry_price' at (or very close to) that strike's live "
        "'premium'. Derive 'target_1'/'target_2'/'stop_loss' from that live premium so the call "
        "is realistic and trackable. Do not invent premiums that contradict the chain."
    ),
    "equity": (
        "Focus on liquid NSE cash stocks (large/mid cap). Recommend BUY or SELL with a cash "
        "price entry, two targets and a stop-loss. Use technical structure (support/resistance, "
        "breakouts, moving averages) plus the news context. Timeframe 'intraday' or 'swing'."
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
        "1. Respond ONLY with a valid JSON ARRAY of call objects. No markdown, no prose.\n"
        "2. Return 0 to 3 of your HIGHEST-CONVICTION ideas. Quality over quantity. "
        "Return [] if nothing is compelling right now.\n"
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
    )


def _build_market_prompt(market_data: dict, headlines: list[str]) -> str:
    mkt = json.dumps(market_data, indent=2, default=str)
    news = "\n".join(f"- {h}" for h in headlines) if headlines else "No fresh headlines."
    return (
        "## Live Market Snapshot\n" + mkt +
        "\n\n## Recent Headlines / Macro\n" + news +
        f"\n\n## Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n\n"
        "Generate your highest-conviction calls now. JSON array only."
    )


def _parse_calls(raw: str) -> list[dict]:
    """Extract a JSON array of calls from the model response."""
    raw = raw.strip()
    # Strip code fences if present
    fence = re.search(r"```(?:json)?\s*(\[.*\])\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        # Fall back to the first [...] block
        arr = re.search(r"(\[.*\])", raw, re.DOTALL)
        if arr:
            raw = arr.group(1)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as e:
        log.error("Failed to parse calls JSON: %s | raw: %s", e, raw[:300])
        return []


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
    prompt = _system_prompt(category) + "\n\n" + _build_market_prompt(market_data, headlines)

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=1024,
                temperature=0.3,
            ),
        )
        raw_calls = _parse_calls(response.text)
    except Exception as e:
        log.error("Call generation failed for %s: %s", category, e)
        return []

    published: list[dict] = []
    for call in raw_calls:
        call["category"] = category

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

    log.info("Generated %s publishable %s calls (from %s candidates)",
             len(published), category, len(raw_calls))
    return published
