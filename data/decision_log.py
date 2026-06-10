"""
decision_log.py — per-call audit trail: the inputs + rationale behind each call.

When the engine publishes a call we append one JSON line capturing exactly what
the LLM was working from for THAT instrument — spot, daily + intraday technicals,
the chosen strike's premium/greeks/OI, chain PCR, macro and the headlines — plus
the model's own rationale and confidence. `/why <call_id>` reads it back.

Stored on the host-mounted logs volume (survives a Postgres reset), same as the
paper-trade history.
"""
import json
import os
import re
from datetime import datetime

from config.settings import LOG_DIR
from config.logger import get_logger

log = get_logger("decision_log")

_PATH = os.path.join(LOG_DIR, "call_decisions.jsonl")
_OPT_RE = re.compile(r"(\d{3,7})\s*(CE|PE)", re.IGNORECASE)


def _chosen_strike(chain: dict | None, instrument: str) -> dict | None:
    """Find the chain row matching the call's strike + option type."""
    m = _OPT_RE.search(instrument or "")
    if not m or not isinstance(chain, dict):
        return None
    strike, ot = float(m.group(1)), m.group(2).upper()
    for r in chain.get("strikes", []):
        if abs(float(r.get("strike", 0)) - strike) < 0.5 and r.get("option_type") == ot:
            return r
    return None


def log_decision(call_id: int, call: dict, market: dict, headlines: list[str]) -> None:
    """Append the decision context for a freshly published call. Best-effort."""
    try:
        und = str(call.get("underlying", "")).upper()
        oc = market.get("option_chain")
        chain = oc.get(und) if isinstance(oc, dict) else None
        rec = {
            "call_id": call_id,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "instrument": call.get("instrument"),
            "underlying": und,
            "action": str(call.get("action", "")).upper(),
            "category": call.get("category"),
            "confidence": call.get("confidence"),
            "rationale": call.get("rationale"),
            "levels": {"entry": call.get("entry_price"), "t1": call.get("target_1"),
                       "t2": call.get("target_2"), "sl": call.get("stop_loss")},
            "inputs": {
                "spot": (chain or {}).get("spot") if chain else market.get(und.lower()),
                "pcr_oi": (chain or {}).get("pcr_oi") if chain else None,
                "chosen_strike": _chosen_strike(chain, call.get("instrument")),
                "daily": (market.get("technicals") or {}).get(und),
                "intraday": (market.get("intraday") or {}).get(und),
                "macro": {k: market.get(k) for k in ("crude_brent", "usd_inr", "dxy") if market.get(k) is not None},
                "headlines": (headlines or [])[:3],
            },
        }
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("Decision log failed for call #%s: %s", call_id, e)


def read_decision(call_id: int) -> dict | None:
    """Return the most recent decision record for a call_id, or None."""
    found = None
    try:
        with open(_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("call_id") == call_id:
                    found = rec  # keep the last (newest) match
    except FileNotFoundError:
        return None
    return found
