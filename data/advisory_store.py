"""
advisory_store.py — PostgreSQL persistence for the advisory product.

Two core tables:
  - calls:       Every trade idea (entry/target/stop-loss) with full lifecycle status.
  - subscribers: Users receiving calls via Telegram (advisory-only, no broker/execution).

A "call" is the central entity of the advisory model:
  category + instrument + action + entry + target(s) + stop-loss + rationale.

Lifecycle:
  active → entry_triggered → target1_hit → target_hit (T2) | sl_hit | expired

Categories: index_option | equity | futures | commodity
"""

import json
import psycopg2
from datetime import datetime, timedelta
import os

from config.logger import get_logger

log = get_logger("advisory_store")

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "trading_agent")
DB_USER = os.getenv("DB_USER", "trading_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "trading_password")

CATEGORIES = ("index_option", "equity", "futures", "commodity")
DEFAULT_CATEGORIES = ",".join(CATEGORIES)

# Terminal statuses (call no longer tracked / scored)
CLOSED_STATUSES = ("target_hit", "sl_hit", "expired", "closed")


# ── Schema ───────────────────────────────────────────────────────────────────

CREATE_CALLS = """
CREATE TABLE IF NOT EXISTS calls (
    id              SERIAL PRIMARY KEY,
    category        TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    underlying      TEXT,
    action          TEXT NOT NULL,
    timeframe       TEXT,
    entry_price     NUMERIC(12, 2),
    entry_min       NUMERIC(12, 2),
    entry_max       NUMERIC(12, 2),
    target_1        NUMERIC(12, 2),
    target_2        NUMERIC(12, 2),
    stop_loss       NUMERIC(12, 2),
    confidence      INTEGER,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    entry_triggered BOOLEAN DEFAULT FALSE,
    last_price      NUMERIC(12, 2),
    result_pct      NUMERIC(8, 2),
    raw_json        TEXT,
    issued_at       TIMESTAMP DEFAULT NOW(),
    expires_at      TIMESTAMP,
    closed_at       TIMESTAMP
);
"""

CREATE_SUBSCRIBERS = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     BIGINT PRIMARY KEY,
    username    TEXT,
    categories  TEXT DEFAULT 'index_option,equity,futures,commodity',
    is_active   BOOLEAN DEFAULT TRUE,
    joined_at   TIMESTAMP DEFAULT NOW()
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status)",
    "CREATE INDEX IF NOT EXISTS idx_calls_category ON calls(category)",
    "CREATE INDEX IF NOT EXISTS idx_calls_issued ON calls(issued_at DESC)",
]


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


def init_advisory_db() -> None:
    """Create advisory tables and indexes (idempotent)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(CREATE_CALLS)
        cur.execute(CREATE_SUBSCRIBERS)
        for idx in INDEXES:
            cur.execute(idx)
        conn.commit()
        log.info("Advisory tables initialised (calls, subscribers)")
    finally:
        cur.close()
        conn.close()


# ── Calls ────────────────────────────────────────────────────────────────────

def save_call(call: dict) -> int:
    """Insert a new call, return its ID.

    Expected keys: category, instrument, underlying, action, timeframe,
    entry_price, entry_min, entry_max, target_1, target_2, stop_loss,
    confidence, rationale, expires_at (optional datetime/iso str).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        expires_at = call.get("expires_at")
        if expires_at is None:
            # Default expiry: intraday calls expire EOD, others in 5 trading days
            tf = (call.get("timeframe") or "intraday").lower()
            if tf == "intraday":
                expires_at = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
            else:
                expires_at = datetime.now() + timedelta(days=5)

        cur.execute(
            """INSERT INTO calls
               (category, instrument, underlying, action, timeframe,
                entry_price, entry_min, entry_max, target_1, target_2, stop_loss,
                confidence, rationale, status, raw_json, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                call.get("category", ""),
                call.get("instrument", ""),
                call.get("underlying"),
                call.get("action", ""),
                call.get("timeframe", "intraday"),
                call.get("entry_price"),
                call.get("entry_min"),
                call.get("entry_max"),
                call.get("target_1"),
                call.get("target_2"),
                call.get("stop_loss"),
                call.get("confidence"),
                call.get("rationale"),
                "active",
                json.dumps(call, default=str),
                expires_at,
            ),
        )
        call_id = cur.fetchone()[0]
        conn.commit()
        log.info("Call #%s saved: %s %s %s", call_id, call.get("action"),
                 call.get("instrument"), call.get("category"))
        return call_id
    finally:
        cur.close()
        conn.close()


def get_active_calls() -> list[dict]:
    """Fetch all calls still being tracked (not in a terminal status)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM calls WHERE status NOT IN %s ORDER BY issued_at DESC",
            (CLOSED_STATUSES,),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_recent_calls(limit: int = 20, category: str | None = None) -> list[dict]:
    """Fetch recent calls, optionally filtered by category."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        if category:
            cur.execute(
                "SELECT * FROM calls WHERE category = %s ORDER BY issued_at DESC LIMIT %s",
                (category, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM calls ORDER BY issued_at DESC LIMIT %s",
                (limit,),
            )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def update_call_status(
    call_id: int,
    status: str,
    last_price: float | None = None,
    result_pct: float | None = None,
    entry_triggered: bool | None = None,
) -> None:
    """Update a call's lifecycle state. Sets closed_at when entering a terminal status."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        fields = ["status = %s"]
        params: list = [status]

        if last_price is not None:
            fields.append("last_price = %s")
            params.append(last_price)
        if result_pct is not None:
            fields.append("result_pct = %s")
            params.append(result_pct)
        if entry_triggered is not None:
            fields.append("entry_triggered = %s")
            params.append(entry_triggered)
        if status in CLOSED_STATUSES:
            fields.append("closed_at = %s")
            params.append(datetime.now())

        params.append(call_id)
        cur.execute(f"UPDATE calls SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def active_instruments() -> set[str]:
    """Set of instrument names that already have an active call (avoid duplicates)."""
    return {c["instrument"] for c in get_active_calls()}


# ── Subscribers ──────────────────────────────────────────────────────────────

def add_subscriber(chat_id: int, username: str = "") -> None:
    """Register a Telegram subscriber (idempotent; reactivates if previously inactive)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO subscribers (chat_id, username, categories, is_active)
               VALUES (%s, %s, %s, TRUE)
               ON CONFLICT (chat_id)
               DO UPDATE SET is_active = TRUE, username = EXCLUDED.username""",
            (chat_id, username, DEFAULT_CATEGORIES),
        )
        conn.commit()
        log.info("Subscriber added/reactivated: %s (%s)", chat_id, username)
    finally:
        cur.close()
        conn.close()


def set_subscriber_active(chat_id: int, active: bool) -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE subscribers SET is_active = %s WHERE chat_id = %s",
            (active, chat_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def set_subscriber_categories(chat_id: int, categories: list[str]) -> None:
    """Set which call categories a subscriber wants."""
    valid = [c for c in categories if c in CATEGORIES]
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE subscribers SET categories = %s WHERE chat_id = %s",
            (",".join(valid), chat_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_subscriber(chat_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM subscribers WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        if not row:
            return None
        columns = [d[0] for d in cur.description]
        return dict(zip(columns, row))
    finally:
        cur.close()
        conn.close()


def get_subscribers_for_category(category: str) -> list[dict]:
    """All active subscribers who want a given category."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT chat_id, username, categories FROM subscribers "
            "WHERE is_active = TRUE AND categories LIKE %s",
            (f"%{category}%",),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_all_active_subscribers() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT chat_id, username, categories FROM subscribers WHERE is_active = TRUE")
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


# ── Performance / Track Record ───────────────────────────────────────────────

def get_track_record(days: int = 30) -> dict:
    """Compute accuracy stats over closed calls in the last `days` days.

    Returns overall + per-category: total, wins, losses, win_rate, avg_return.
    A "win" = call closed with status target1_hit/target_hit (positive result).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT category, status, result_pct
               FROM calls
               WHERE status IN %s
                 AND closed_at >= NOW() - make_interval(days => %s)""",
            (CLOSED_STATUSES, days),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    def blank():
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_return": 0.0, "_sum_return": 0.0}

    overall = blank()
    by_cat: dict[str, dict] = {c: blank() for c in CATEGORIES}

    for category, status, result_pct in rows:
        bucket = by_cat.get(category, overall)
        for b in (overall, bucket):
            b["total"] += 1
            ret = float(result_pct) if result_pct is not None else 0.0
            b["_sum_return"] += ret
            if status in ("target_hit", "target1_hit") or ret > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1

    for b in (overall, *by_cat.values()):
        if b["total"]:
            b["win_rate"] = round(100.0 * b["wins"] / b["total"], 1)
            b["avg_return"] = round(b["_sum_return"] / b["total"], 2)
        b.pop("_sum_return", None)

    return {"window_days": days, "overall": overall, "by_category": by_cat}
