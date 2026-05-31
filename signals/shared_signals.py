"""
shared_signals.py — Market-wide signal generation for all users.

Unlike portfolio-specific signals (in claude_engine.py), shared signals focus on:
- Macro trends: sector rotation, index movement, geopolitical impact
- Market-wide hedging strategies
- Entry/exit setup recommendations (independent of individual portfolios)
- Broadcast to all users in 'shared' or 'both' signal mode

Example shared signal:
"NIFTY50 showing strong resistance at 22,500. Prepare for sector rotation
towards energy and defensive sectors. Consider long puts on IT sector."
"""

import json
import re
from google import genai

from config.settings import GEMINI_API_KEY, GEMINI_MODEL
from config.logger import get_logger

log = get_logger("shared_signals")

_client = genai.Client(api_key=GEMINI_API_KEY)

SHARED_SIGNAL_SYSTEM_PROMPT = (
    "You are an expert market analyst for Indian F&O markets (NSE).\n"
    "Analyze MARKET-WIDE conditions (NOT individual portfolios) to identify trading setups.\n\n"
    "RULES:\n"
    "1. Respond ONLY with a valid JSON object -- no markdown, no text outside JSON.\n"
    '2. If no actionable setup, return: {"setup": "HOLD", "reason": "...", "confidence": 0}\n'
    "3. Setups: SECTOR_ROTATION | INDEX_BREAKOUT | HEDGING_PLAY | EARNINGS_SEASON | "
    "VOLATILITY_EXPANSION | SUPPORT_TEST | RESISTANCE_TEST | HOLD\n"
    "4. Always include: setup, instrument (index or sector ETF), action (BUY/SELL/HEDGE),\n"
    "   reason, urgency (low/medium/high), confidence (0-100), suggested_expiry (weekly/monthly)\n"
    "5. Focus on: macro trends, Fed policy impact, FII/DII flows, geopolitical events.\n"
    "6. Suggest specific instruments anyone can trade (NIFTY, BANKNIFTY, FINNIFTY, sector ETFs).\n"
    "7. Risk-first: suggest hedges before recommending aggressive positions.\n"
)


def _build_shared_market_prompt(market_data: dict, headlines: list[str]) -> str:
    """Build market-wide prompt (no portfolio positions)."""
    mkt_text = json.dumps(market_data, indent=2)
    news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "No news available."

    return (
        "## Market Snapshot (Current)\n" + mkt_text +
        "\n\n## Recent Headlines & Macro Events\n" + news_text +
        "\n\nAnalyze the market environment. What trading setup should all traders watch?\n"
        "Suggest a market-wide action. Respond with JSON only."
    )


def _parse_shared_signal(raw: str) -> dict | None:
    """Parse shared signal JSON response."""
    raw = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    try:
        data = json.loads(raw)
        if "setup" not in data:
            log.warning("Shared signal missing 'setup' key: %s", raw[:200])
            return None
        return data
    except json.JSONDecodeError as e:
        log.error("Failed to parse shared signal JSON: %s | raw: %s", e, raw[:300])
        return None


async def get_shared_signal(market_data: dict, headlines: list[str]) -> dict | None:
    """
    Generate a market-wide trading signal.

    Args:
        market_data: Dict with NIFTY50, BANKNIFTY, FINNIFTY, VIX, USD/INR, commodities
        headlines: List of recent market news headlines

    Returns:
        Shared signal dict with setup, instrument, action, reason, confidence, etc.
        Returns None if Gemini call fails or no setup identified.

    Example response:
    {
        "setup": "SECTOR_ROTATION",
        "instrument": "FINNIFTY",
        "action": "BUY_CALL",
        "reason": "FII outflows impacting IT, but tech recovery expected on US Fed pause",
        "urgency": "high",
        "confidence": 72,
        "suggested_expiry": "weekly",
        "strike_guidance": "Buy 2-3% OTM calls",
        "hedge": "Pair with long puts on BANKNIFTY"
    }
    """
    if not market_data:
        log.info("No market data -- skipping shared signal generation")
        return None

    prompt = SHARED_SIGNAL_SYSTEM_PROMPT + "\n\n" + _build_shared_market_prompt(market_data, headlines)

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=512,
                temperature=0.3,  # Slightly higher temp for broader market perspective
            ),
        )
        raw = response.text
        log.debug("Shared signal raw response: %s", raw[:500])
        signal = _parse_shared_signal(raw)

        if signal:
            log.info(
                "Shared Signal Generated: setup=%s | instrument=%s | confidence=%s | urgency=%s",
                signal.get("setup"),
                signal.get("instrument", ""),
                signal.get("confidence"),
                signal.get("urgency"),
            )
        return signal
    except Exception as e:
        log.error("Shared signal generation failed: %s", e)
        return None
