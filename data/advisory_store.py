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

# Which categories the engine generates + tracks. Configurable via
# ADVISORY_CATEGORIES (default: index options only). Anything not listed here is
# never generated, published, or paper-traded.
CATEGORIES = tuple(
    c.strip() for c in os.getenv("ADVISORY_CATEGORIES", "index_option").split(",") if c.strip()
)
DEFAULT_CATEGORIES = ",".join(CATEGORIES)

# Terminal statuses (call no longer tracked / scored)
CLOSED_STATUSES = ("target_hit", "sl_hit", "expired", "closed")

# Paper books are scoped by `strategy`. The original single account migrates to this
# default, so every legacy row + back-compat call keeps working unchanged.
DEFAULT_STRATEGY = "opt_buy"


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
    closed_at       TIMESTAMP,
    option_expiry   DATE
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

CREATE_PAPER_ACCOUNT = """
CREATE TABLE IF NOT EXISTS paper_account (
    id               INTEGER PRIMARY KEY,
    starting_capital NUMERIC(14, 2) NOT NULL,
    cash             NUMERIC(14, 2) NOT NULL,
    realized_pnl     NUMERIC(14, 2) NOT NULL DEFAULT 0,
    peak_equity      NUMERIC(14, 2) NOT NULL,
    max_drawdown_pct NUMERIC(8, 2) NOT NULL DEFAULT 0,
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);
"""

CREATE_PAPER_POSITIONS = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id            SERIAL PRIMARY KEY,
    call_id       INTEGER,
    instrument    TEXT NOT NULL,
    underlying    TEXT,
    category      TEXT,
    action        TEXT NOT NULL,
    lot_size      INTEGER NOT NULL,
    lots          INTEGER NOT NULL,
    quantity      INTEGER NOT NULL,
    remaining_qty INTEGER NOT NULL,
    entry_price   NUMERIC(12, 2) NOT NULL,
    last_price    NUMERIC(12, 2),
    realized_pnl  NUMERIC(14, 2) NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',
    opened_at     TIMESTAMP DEFAULT NOW(),
    closed_at     TIMESTAMP
);
"""

CREATE_PAPER_FILLS = """
CREATE TABLE IF NOT EXISTS paper_fills (
    id           SERIAL PRIMARY KEY,
    position_id  INTEGER,
    call_id      INTEGER,
    instrument   TEXT,
    kind         TEXT,
    qty          INTEGER,
    price        NUMERIC(12, 2),
    realized_pnl NUMERIC(14, 2) DEFAULT 0,
    ts           TIMESTAMP DEFAULT NOW()
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status)",
    "CREATE INDEX IF NOT EXISTS idx_calls_category ON calls(category)",
    "CREATE INDEX IF NOT EXISTS idx_calls_issued ON calls(issued_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_paper_pos_status ON paper_positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_pos_call ON paper_positions(call_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_fills_ts ON paper_fills(ts)",
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
        cur.execute(CREATE_PAPER_ACCOUNT)
        cur.execute(CREATE_PAPER_POSITIONS)
        cur.execute(CREATE_PAPER_FILLS)
        # Migration for pre-existing DBs that lack newer columns.
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS option_expiry DATE")
        # paper_status: how the paper engine treated this call —
        #   'executed' | 'unfunded' (no free capital) | 'capped' (max open) |
        #   'halted_daily_loss'. NULL = not a paper-scope call / not evaluated.
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS paper_status TEXT")
        # Profit-protection bookkeeping: profit_since = when the call first held a
        # meaningful unrealized profit (drives the time-in-profit partial);
        # partial_booked = a profit partial has already been taken on this call.
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS profit_since TIMESTAMP")
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS partial_booked BOOLEAN DEFAULT FALSE")
        # Stage timestamps — drive the "time in stage" display (waiting / in-trade / T1 trail).
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS entry_triggered_at TIMESTAMP")
        cur.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS target1_hit_at TIMESTAMP")
        # Multi-strategy paper books: every paper row is scoped by `strategy`, and the
        # account tracks blocked `margin_used` (selling/spreads block margin; the old
        # long-only model never needed it). The existing single account → 'opt_buy'.
        cur.execute("ALTER TABLE paper_account ADD COLUMN IF NOT EXISTS strategy TEXT")
        cur.execute("ALTER TABLE paper_account ADD COLUMN IF NOT EXISTS margin_used NUMERIC(14,2) NOT NULL DEFAULT 0")
        # Backfill the canonical legacy account (lowest id == the historical single
        # account) to 'opt_buy'. Any OTHER pre-existing rows are stale duplicates from
        # the single-account era (e.g. an orphaned reset) — tag them uniquely so the
        # unique index can build and they never collide. Doing this in two steps (vs.
        # a blanket UPDATE → 'opt_buy') keeps the migration non-destructive AND
        # idempotent: a re-run finds no NULL rows left, so it can't re-collide.
        cur.execute(
            f"UPDATE paper_account SET strategy = '{DEFAULT_STRATEGY}' "
            f"WHERE strategy IS NULL AND id = (SELECT MIN(id) FROM paper_account)"
        )
        cur.execute("UPDATE paper_account SET strategy = 'legacy_' || id WHERE strategy IS NULL")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_account_strategy ON paper_account(strategy)")
        cur.execute(f"ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT '{DEFAULT_STRATEGY}'")
        cur.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS premium_received NUMERIC(14,2) DEFAULT 0")
        cur.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS margin_blocked NUMERIC(14,2) DEFAULT 0")
        cur.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS structure TEXT DEFAULT 'single'")
        cur.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS legs TEXT")  # json.dumps(spread legs)
        cur.execute(f"ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT '{DEFAULT_STRATEGY}'")
        cur.execute(f"ALTER TABLE calls ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT '{DEFAULT_STRATEGY}'")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_pos_strategy ON paper_positions(strategy)")
        for idx in INDEXES:
            cur.execute(idx)
        conn.commit()
        log.info("Advisory tables initialised (calls, subscribers, paper_*)")
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
                confidence, rationale, status, raw_json, expires_at, option_expiry, strategy)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                call.get("option_expiry"),
                call.get("strategy", DEFAULT_STRATEGY),
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


def get_closed_calls(limit: int = 50) -> list[dict]:
    """Calls that have reached a terminal status, newest first (for the dashboard)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM calls WHERE status IN %s ORDER BY issued_at DESC LIMIT %s",
            (CLOSED_STATUSES, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_call(call_id: int) -> dict | None:
    """Fetch a single call by id (any status), or None."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
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
    stop_loss: float | None = None,
) -> None:
    """Update a call's lifecycle state. Sets closed_at when entering a terminal status.

    `stop_loss` lets the tracker trail the SL (e.g. to breakeven after T1) so the
    new level drives subsequent exit evaluation.
    """
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
            if entry_triggered:  # stamp the entry time once (drives the in-trade timer)
                fields.append("entry_triggered_at = COALESCE(entry_triggered_at, NOW())")
        if status == "target1_hit":  # stamp the T1-trail start once
            fields.append("target1_hit_at = COALESCE(target1_hit_at, NOW())")
        if stop_loss is not None:
            fields.append("stop_loss = %s")
            params.append(stop_loss)
        if status in CLOSED_STATUSES:
            fields.append("closed_at = %s")
            params.append(datetime.now())

        params.append(call_id)
        cur.execute(f"UPDATE calls SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
    finally:
        cur.close()
        conn.close()


_KEEP = object()  # sentinel: "leave this column unchanged"


def set_call_profit_state(call_id: int, profit_since=_KEEP, partial_booked=_KEEP) -> None:
    """Persist profit-protection bookkeeping on a call (profit_since / partial_booked)."""
    fields: list[str] = []
    params: list = []
    if profit_since is not _KEEP:
        fields.append("profit_since = %s")
        params.append(profit_since)
    if partial_booked is not _KEEP:
        fields.append("partial_booked = %s")
        params.append(partial_booked)
    if not fields:
        return
    params.append(call_id)
    conn = get_conn()
    cur = conn.cursor()
    try:
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


# ── Paper trading (shadow mode) ───────────────────────────────────────────────

def ensure_paper_account(starting_capital: float, strategy: str = DEFAULT_STRATEGY) -> dict:
    """Create the paper account for `strategy` if absent; return it."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM paper_account WHERE strategy = %s", (strategy,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """INSERT INTO paper_account
                   (id, strategy, starting_capital, cash, realized_pnl, peak_equity)
                   VALUES (COALESCE((SELECT MAX(id) FROM paper_account), 0) + 1, %s, %s, %s, 0, %s)""",
                (strategy, starting_capital, starting_capital, starting_capital),
            )
            conn.commit()
            cur.execute("SELECT * FROM paper_account WHERE strategy = %s", (strategy,))
            row = cur.fetchone()
            log.info("Paper account [%s] created with ₹%.2f starting capital", strategy, starting_capital)
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        cur.close()
        conn.close()


def reset_paper_account(starting_capital: float, strategy: str = DEFAULT_STRATEGY) -> None:
    """DESTRUCTIVE: wipe THIS strategy's paper positions/fills + account, recreate fresh.

    Scoped to `strategy` so resetting one book never touches another. Closed-trade
    history in logs/paper_history.jsonl is untouched; this book's per-call paper_status
    flags are cleared.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM paper_fills WHERE strategy = %s", (strategy,))
        cur.execute("DELETE FROM paper_positions WHERE strategy = %s", (strategy,))
        cur.execute("DELETE FROM paper_account WHERE strategy = %s", (strategy,))
        cur.execute(
            """INSERT INTO paper_account (id, strategy, starting_capital, cash, realized_pnl, peak_equity)
               VALUES (COALESCE((SELECT MAX(id) FROM paper_account), 0) + 1, %s, %s, %s, 0, %s)""",
            (strategy, starting_capital, starting_capital, starting_capital),
        )
        cur.execute("UPDATE calls SET paper_status = NULL WHERE strategy = %s AND paper_status IS NOT NULL", (strategy,))
        conn.commit()
        log.warning("PAPER ACCOUNT [%s] RESET to ₹%.0f (positions + fills wiped).", strategy, starting_capital)
    finally:
        cur.close()
        conn.close()


def set_call_paper_status(call_id: int, status: str) -> None:
    """Record how the paper engine treated a call (executed / unfunded / capped / …)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE calls SET paper_status = %s WHERE id = %s", (status, call_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_not_executed_calls(limit: int = 60) -> list[dict]:
    """Calls the paper engine generated but did NOT execute (capital/limits)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM calls WHERE paper_status IN "
            "('unfunded', 'capped', 'halted_daily_loss', 'risk_skip') ORDER BY issued_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_paper_account(strategy: str = DEFAULT_STRATEGY) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM paper_account WHERE strategy = %s", (strategy,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        cur.close()
        conn.close()


def get_open_paper_positions(strategy: str = DEFAULT_STRATEGY) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' AND strategy = %s ORDER BY opened_at",
            (strategy,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_closed_paper_positions(limit: int = 15, strategy: str = DEFAULT_STRATEGY) -> list[dict]:
    """Most-recently-closed paper positions, newest first (for the /paper ledger)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM paper_positions WHERE status = 'closed' AND strategy = %s "
            "ORDER BY closed_at DESC NULLS LAST LIMIT %s",
            (strategy, limit),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_call_levels(call_ids: list) -> dict:
    """Map call_id → {target_1, target_2, stop_loss, entry_price} for display."""
    ids = [int(c) for c in call_ids if c is not None]
    if not ids:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, target_1, target_2, stop_loss, entry_price, option_expiry "
            "FROM calls WHERE id = ANY(%s)",
            (ids,),
        )
        return {
            r[0]: {"target_1": r[1], "target_2": r[2], "stop_loss": r[3],
                   "entry_price": r[4], "option_expiry": r[5]}
            for r in cur.fetchall()
        }
    finally:
        cur.close()
        conn.close()


def get_paper_fills_on(date_str: str) -> list[dict]:
    """Every realized booking (partial/exit/stop/expiry) on a date, with the
    position's entry price + side. Summing realized_pnl here equals the account's
    realized for that day — it includes partials booked on still-open positions."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT f.ts, f.instrument, f.kind, f.qty, f.price, f.realized_pnl,
                      p.entry_price, p.action
               FROM paper_fills f
               LEFT JOIN paper_positions p ON p.id = f.position_id
               WHERE f.ts::date = %s AND f.kind <> 'entry'
               ORDER BY f.ts""",
            (date_str,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        cur.close()
        conn.close()


def get_open_paper_position_by_call(call_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM paper_positions WHERE call_id = %s AND status = 'open' LIMIT 1",
            (call_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        cur.close()
        conn.close()


def open_paper_position(
    *, call_id, instrument, underlying, category, action,
    lot_size, lots, entry_price, cash_delta,
    strategy=DEFAULT_STRATEGY, premium_received=0.0, margin_blocked=0.0,
    structure="single", legs=None,
) -> int:
    """Open a simulated position and apply the entry cashflow + blocked margin atomically.

    For a long, cash_delta is negative (premium deployed) and margin_blocked is 0.
    For a short/spread, cash_delta is the premium RECEIVED (positive) and
    margin_blocked is the SPAN/max-loss margin held against the position.
    """
    import json as _json
    qty = lots * lot_size
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO paper_positions
               (call_id, instrument, underlying, category, action, lot_size, lots,
                quantity, remaining_qty, entry_price, last_price, status,
                strategy, premium_received, margin_blocked, structure, legs)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s) RETURNING id""",
            (call_id, instrument, underlying, category, action, lot_size, lots,
             qty, qty, entry_price, entry_price,
             strategy, premium_received, margin_blocked, structure,
             _json.dumps(legs) if legs is not None else None),
        )
        pos_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE paper_account SET cash = cash + %s, margin_used = margin_used + %s, "
            "updated_at = NOW() WHERE strategy = %s",
            (cash_delta, margin_blocked, strategy),
        )
        cur.execute(
            """INSERT INTO paper_fills (position_id, call_id, instrument, kind, qty, price, realized_pnl, strategy)
               VALUES (%s,%s,%s,'entry',%s,%s,0,%s)""",
            (pos_id, call_id, instrument, qty, entry_price, strategy),
        )
        conn.commit()
        return pos_id
    finally:
        cur.close()
        conn.close()


def book_paper_exit(
    *, position_id, call_id, instrument, exit_qty, exit_price,
    realized_delta, cash_delta, kind, fully_closed,
    strategy=DEFAULT_STRATEGY, margin_release=0.0,
) -> None:
    """Reduce/close a position, apply exit cashflow, realized P&L and margin release atomically."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        if fully_closed:
            cur.execute(
                """UPDATE paper_positions
                   SET remaining_qty = remaining_qty - %s, realized_pnl = realized_pnl + %s,
                       last_price = %s, status = 'closed', closed_at = NOW()
                   WHERE id = %s""",
                (exit_qty, realized_delta, exit_price, position_id),
            )
        else:
            cur.execute(
                """UPDATE paper_positions
                   SET remaining_qty = remaining_qty - %s, realized_pnl = realized_pnl + %s,
                       last_price = %s
                   WHERE id = %s""",
                (exit_qty, realized_delta, exit_price, position_id),
            )
        cur.execute(
            "UPDATE paper_account SET cash = cash + %s, realized_pnl = realized_pnl + %s, "
            "margin_used = GREATEST(margin_used - %s, 0), updated_at = NOW() WHERE strategy = %s",
            (cash_delta, realized_delta, margin_release, strategy),
        )
        cur.execute(
            """INSERT INTO paper_fills (position_id, call_id, instrument, kind, qty, price, realized_pnl, strategy)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (position_id, call_id, instrument, kind, exit_qty, exit_price, realized_delta, strategy),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def set_paper_position_last_price(position_id: int, last_price: float) -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE paper_positions SET last_price = %s WHERE id = %s",
            (last_price, position_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def compute_paper_equity(strategy: str = DEFAULT_STRATEGY) -> float:
    """Mark-to-market equity = cash + Σ(signed remaining qty × last price).

    Sign-based, so it is already correct for shorts: a short's cash includes the
    premium received at entry and the −qty×price term is the cost to buy it back.
    """
    acct = get_paper_account(strategy)
    if not acct:
        return 0.0
    equity = float(acct["cash"])
    for p in get_open_paper_positions(strategy):
        price = float(p["last_price"]) if p["last_price"] is not None else float(p["entry_price"])
        sign = 1 if str(p["action"]).upper() == "BUY" else -1
        equity += sign * int(p["remaining_qty"]) * price
    return round(equity, 2)


def record_paper_equity(equity: float, strategy: str = DEFAULT_STRATEGY) -> None:
    """Track peak equity and max drawdown from the current mark-to-market equity."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT peak_equity, max_drawdown_pct FROM paper_account WHERE strategy = %s", (strategy,))
        r = cur.fetchone()
        if not r:
            return
        peak = max(float(r[0]), equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        mdd = max(float(r[1]), dd)
        cur.execute(
            "UPDATE paper_account SET peak_equity = %s, max_drawdown_pct = %s, updated_at = NOW() WHERE strategy = %s",
            (round(peak, 2), round(mdd, 2), strategy),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_paper_today_realized(strategy: str = DEFAULT_STRATEGY) -> float:
    """Sum of realized P&L booked today for this book (for the daily-loss guardrail)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_fills "
            "WHERE ts::date = CURRENT_DATE AND strategy = %s",
            (strategy,),
        )
        return float(cur.fetchone()[0] or 0.0)
    finally:
        cur.close()
        conn.close()


def get_paper_stats(strategy: str = DEFAULT_STRATEGY) -> dict:
    """Summary of a paper book: equity, returns, P&L, win rate, drawdown, margin."""
    acct = get_paper_account(strategy)
    if not acct:
        return {}
    equity = compute_paper_equity(strategy)
    start = float(acct["starting_capital"])
    margin_used = float(acct.get("margin_used") or 0.0)

    open_positions = get_open_paper_positions(strategy)
    unrealized = 0.0
    deployed = 0.0  # capital locked in open long positions (premium/price × qty)
    for p in open_positions:
        price = float(p["last_price"]) if p["last_price"] is not None else float(p["entry_price"])
        sign = 1 if str(p["action"]).upper() == "BUY" else -1
        unrealized += sign * int(p["remaining_qty"]) * (price - float(p["entry_price"]))
        deployed += int(p["remaining_qty"]) * float(p["entry_price"])

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT realized_pnl FROM paper_positions WHERE status = 'closed' AND strategy = %s",
            (strategy,),
        )
        closed = [float(r[0]) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    wins = sum(1 for p in closed if p > 0)
    total = len(closed)
    return {
        "strategy": strategy,
        "starting_capital": start,
        "cash": float(acct["cash"]),
        "free_cash": round(float(acct["cash"]) - margin_used, 2),
        "margin_used": round(margin_used, 2),
        "deployed_capital": round(deployed, 2),
        "equity": equity,
        "total_return_pct": round((equity - start) / start * 100, 2) if start else 0.0,
        "realized_pnl": float(acct["realized_pnl"]),
        "unrealized_pnl": round(unrealized, 2),
        "today_realized": get_paper_today_realized(strategy),
        "open_positions": len(open_positions),
        "closed_trades": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(100.0 * wins / total, 1) if total else 0.0,
        "max_drawdown_pct": float(acct["max_drawdown_pct"]),
    }


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
