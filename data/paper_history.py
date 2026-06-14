"""
paper_history.py — durable, file-based ledger of closed paper trades.

Every paper exit (partial / target / stop / expiry / EOD square-off) is appended
here as one JSON line. Unlike the Postgres `paper_positions` table — which is
WIPED on each capital reset (daily/weekly fresh start) — this file is never
reset, so it is the source of truth for performance tracking ACROSS resets:
the dashboard's Daily and Weekly P&L tables and the Telegram /paper digest all
read from here.

One trade can produce several rows (a T1 partial + the final exit). Net P&L is
the sum of all rows; win/loss COUNTS group rows by `position_id` so a partial +
its final exit count as a single position, not two.
"""

import json
import os
from datetime import datetime, timedelta

from config.logger import get_logger
from config.settings import LOG_DIR, PAPER_START_CAPITAL

log = get_logger("paper_history")

HISTORY_PATH = os.path.join(LOG_DIR, "paper_history.jsonl")


def append_history(rec: dict) -> None:
    """Append one closed-trade record (JSON line) to the durable history."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("Paper history append failed: %s", e)


def _read_records() -> list[dict]:
    """All closed-trade records (oldest first). Missing file → empty list."""
    out: list[dict] = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


def read_paper_day(date_str: str) -> dict:
    """Aggregate the persisted closed trades for one date (YYYY-MM-DD)."""
    trades = [r for r in _read_records() if r.get("date") == date_str]
    pnl = round(sum(float(t.get("pnl", 0) or 0) for t in trades), 2)
    wins = sum(1 for t in trades if float(t.get("pnl", 0) or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0) or 0) < 0)
    return {
        "date": date_str,
        "trades": trades,
        "pnl": pnl,
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(100.0 * wins / (wins + losses), 1) if (wins + losses) else 0.0,
        "return_pct": round(pnl / PAPER_START_CAPITAL * 100, 2) if PAPER_START_CAPITAL else 0.0,
    }


def _week_start(date_str: str) -> str | None:
    """Monday (ISO date) of the week containing `date_str` (YYYY-MM-DD)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d - timedelta(days=d.weekday())).isoformat()


def _aggregate(records: list[dict], key_fn) -> list[dict]:
    """Group records by key_fn(record) and roll up P&L stats per bucket.

    Counts (wins/losses/trades) are per POSITION (rows sharing a position_id are
    one trade); net P&L is the sum of every row. Legacy rows with no position_id
    each count as their own position. Returns buckets newest-key first.
    """
    buckets: dict[str, dict] = {}
    for i, r in enumerate(records):
        k = key_fn(r)
        if k is None:
            continue
        b = buckets.setdefault(k, {"net": 0.0, "pos": {}})
        pnl = float(r.get("pnl") or 0)
        b["net"] += pnl
        pid = r.get("position_id")
        gid = pid if pid is not None else f"_r{i}"  # legacy: one row = one trade
        b["pos"][gid] = b["pos"].get(gid, 0.0) + pnl

    out: list[dict] = []
    for k in sorted(buckets, reverse=True):
        pls = list(buckets[k]["pos"].values())
        trades = len(pls)
        wins = sum(1 for v in pls if v > 0)
        losses = sum(1 for v in pls if v < 0)
        out.append({
            "period": k,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
            "gross_profit": round(sum(v for v in pls if v > 0), 2),
            "gross_loss": round(sum(v for v in pls if v < 0), 2),
            "net_pnl": round(buckets[k]["net"], 2),
        })
    return out


def daily_pnl(days: int = 30) -> list[dict]:
    """Per-day P&L breakdown over the durable history (newest day first)."""
    return _aggregate(_read_records(), lambda r: r.get("date"))[:days]


def weekly_pnl(weeks: int = 12) -> list[dict]:
    """Per-week (Mon–Sun) P&L breakdown over the durable history (newest first).

    `period` is the Monday (ISO date) that starts the week.
    """
    return _aggregate(_read_records(), lambda r: _week_start(r.get("date", "")))[:weeks]
