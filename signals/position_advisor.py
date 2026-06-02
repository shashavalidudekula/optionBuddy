"""
position_advisor.py — AI review of the user's CURRENT open positions.

Unlike advisory_engine (which scans the market for fresh trade ideas), this
takes the positions a user already holds and asks the LLM for a next-session
action plan per position: HOLD / EXIT / BOOK_PARTIAL / ADD / HEDGE, with levels
and a short rationale, plus an overall portfolio note.

Advisory only — informational, never executed.
"""

import json
import re
from datetime import datetime

from config.logger import get_logger
from core.llm import generate  # provider-agnostic (Azure/OpenAI/Gemini)

log = get_logger("position_advisor")

_VALID_ACTIONS = {"HOLD", "EXIT", "BOOK_PARTIAL", "ADD", "HEDGE"}


def _system_prompt() -> str:
    return (
        "You are a SEBI-style research analyst reviewing a trader's CURRENT open "
        "F&O / equity positions and advising what to do in the NEXT trading session. "
        "You are NOT scanning for new ideas — only assessing the positions provided.\n\n"
        "For each position choose ONE action:\n"
        "  HOLD          — keep as-is into the next session\n"
        "  EXIT          — close the full position\n"
        "  BOOK_PARTIAL  — book part of the position, trail the rest\n"
        "  ADD           — average / add to the position\n"
        "  HEDGE         — protect with an offsetting option/future\n\n"
        "RULES:\n"
        "1. Respond ONLY with a valid JSON OBJECT. No markdown, no prose outside the JSON.\n"
        "2. Exact shape:\n"
        '   {\n'
        '     "positions": [\n'
        '       {"instrument": "<exact label as given>",\n'
        '        "action": "HOLD|EXIT|BOOK_PARTIAL|ADD|HEDGE",\n'
        '        "confidence": <0-100 integer>,\n'
        '        "stop_loss": <number or null>, "target": <number or null>,\n'
        '        "reason": "1-2 sentence rationale"}\n'
        '     ],\n'
        '     "overall": "2-3 sentence portfolio-level note for tomorrow"\n'
        '   }\n'
        "3. Use the live LTP/premium and unrealised P&L provided to be specific and realistic; "
        "for options respect theta decay and the time left to expiry.\n"
        "4. Risk-first: protect capital, flag concentrated or oversized risk, prefer trailing "
        "stops once a position is in profit.\n"
        "5. Cover EVERY position you are given, reusing its exact instrument label.\n"
    )


def _build_prompt(positions: list[dict], market_data: dict, headlines: list[str]) -> str:
    pos_json = json.dumps(positions, indent=2, default=str)
    mkt = json.dumps(market_data, indent=2, default=str)
    news = "\n".join(f"- {h}" for h in headlines) if headlines else "No fresh headlines."
    return (
        "## My Open Positions\n" + pos_json +
        "\n\n## Live Market Snapshot\n" + mkt +
        "\n\n## Recent Headlines / Macro\n" + news +
        f"\n\n## Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n\n"
        "Review my positions and give the next-session action plan. JSON object only."
    )


def _parse_review(raw: str) -> dict:
    """Extract the JSON object from the model response."""
    raw = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        obj = re.search(r"(\{.*\})", raw, re.DOTALL)
        if obj:
            raw = obj.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Failed to parse review JSON: %s | raw: %s", e, raw[:300])
        return {}
    return data if isinstance(data, dict) else {}


def review_positions(positions: list[dict], market_data: dict, headlines: list[str]) -> dict:
    """Ask Gemini for a next-session action plan over the given open positions.

    Returns {"positions": [...], "overall": str}. Propagates LLMQuotaError
    (quota/rate wall) and other LLM errors so the caller can show a clear
    message instead of silently returning nothing.
    """
    if not positions:
        return {"positions": [], "overall": "No open positions to review."}

    from config.settings import AZURE_OPENAI_DEPLOYMENT_REVIEW

    system = _system_prompt()
    user = _build_prompt(positions, market_data, headlines)
    # /review favours reasoning over cost — use the heavier deployment when set.
    text = generate(user, system=system, json_mode=True, max_tokens=1024, temperature=0.3,
                    model=AZURE_OPENAI_DEPLOYMENT_REVIEW or None)
    review = _parse_review(text)

    out_positions: list[dict] = []
    for item in (review.get("positions", []) if isinstance(review, dict) else []):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).upper().strip().replace(" ", "_")
        item["action"] = action if action in _VALID_ACTIONS else "HOLD"
        out_positions.append(item)

    overall = str(review.get("overall", "")).strip() if isinstance(review, dict) else ""
    log.info("Position review produced %s action(s)", len(out_positions))
    return {"positions": out_positions, "overall": overall}
