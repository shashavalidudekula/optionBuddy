"""
signal_engine.py -- Send market context to Gemini, get a structured trading signal

Free tier: gemini-1.5-flash -- 1,500 requests/day, no credit card needed.
Get key: aistudio.google.com -> Get API key
"""
import json
import re

from google import genai

from config.settings import GEMINI_API_KEY, GEMINI_MODEL
from config.logger import get_logger

log = get_logger("signal_engine")

_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = (
    "You are an expert F&O trading analyst for Indian markets (NSE).\n"
    "Analyze open F&O positions (options/futures on indices, stocks, commodities) and market context to decide the best action.\n\n"
    "RULES:\n"
    "1. Respond ONLY with a valid JSON object -- no markdown, no text outside JSON.\n"
    '2. If no action needed, return: {"signal": "HOLD", "reason": "...", "confidence": 0}\n'
    "3. Signals: HOLD | EXIT | EXIT_LONG | EXIT_SHORT | BUY_CALL | BUY_PUT | REDUCE_LOTS | TAKE_PROFIT\n"
    "4. Always include: signal, instrument, reason, urgency (low/medium/high), confidence (0-100),\n"
    "   max_loss_if_held (INR), action_benefit (INR), key_risk, watch_level\n"
    "5. confidence >= 75 qualifies for auto-execution.\n"
    "6. For options: consider theta decay, delta, IV, days to expiry. For futures: trend, support/resistance.\n"
    "7. Risk-first: protecting capital > maximising profit. Always account for slippage and execution delays.\n"
)


def _build_user_prompt(positions, market, headlines):
    pos_text = json.dumps(positions, indent=2) if positions else "No open positions."
    mkt_text = json.dumps(market, indent=2)
    news_text = "\n".join("- " + h for h in headlines) if headlines else "No relevant news."
    return (
        "## Current Open Positions\n" + pos_text +
        "\n\n## Market Snapshot\n" + mkt_text +
        "\n\n## Recent Headlines\n" + news_text +
        "\n\nAnalyze the above. What should I do right now? Respond with JSON only."
    )


def _parse_signal(raw):
    raw = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    try:
        data = json.loads(raw)
        if "signal" not in data:
            log.warning("Response missing 'signal' key: %s", raw[:200])
            return None
        return data
    except json.JSONDecodeError as e:
        log.error("Failed to parse Gemini JSON: %s | raw: %s", e, raw[:300])
        return None


def get_signal(positions, market, headlines):
    """Call Gemini with current context. Returns signal dict or None."""
    if not positions:
        log.info("No open positions -- skipping Gemini call.")
        return None

    prompt = SYSTEM_PROMPT + "\n\n" + _build_user_prompt(positions, market, headlines)

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=512,
                temperature=0.2,
            ),
        )
        raw = response.text
        log.debug("Gemini raw response: %s", raw[:500])
        signal = _parse_signal(raw)
        if signal:
            log.info(
                "Signal: %s | %s | confidence=%s | urgency=%s",
                signal.get("signal"),
                signal.get("instrument", ""),
                signal.get("confidence"),
                signal.get("urgency"),
            )
        return signal
    except Exception as e:
        log.error("Gemini API error: %s", e)
        return None
