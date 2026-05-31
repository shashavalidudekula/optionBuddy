"""
store.py — PostgreSQL persistence: signals history, P&L log, trade journal

Tables:
  - signals: every Claude signal emitted
  - trades: executed/approved trades
  - pnl_snapshots: daily P&L snapshots
"""
import json
import psycopg2
from datetime import datetime
import os

from config.logger import get_logger

log = get_logger("store")

# Database connection parameters
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "trading_agent")
DB_USER = os.getenv("DB_USER", "trading_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "trading_password")

# ── Schema ─────────────────────────────────────────────────────────────────────

CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id          SERIAL PRIMARY KEY,
    ts          TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    signal      TEXT NOT NULL,
    confidence  INTEGER,
    urgency     TEXT,
    reason      TEXT,
    raw_json    TEXT,
    acted_on    SMALLINT DEFAULT 0,
    outcome     TEXT
);
"""

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    ts              TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    txn_type        TEXT NOT NULL,
    quantity        INTEGER,
    price           NUMERIC(12, 2),
    order_id        TEXT,
    signal_id       INTEGER,
    pnl_realised    NUMERIC(12, 2),
    notes           TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
"""

CREATE_PNL = """
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id              SERIAL PRIMARY KEY,
    ts              TEXT NOT NULL,
    unrealised      NUMERIC(12, 2),
    realised        NUMERIC(12, 2),
    net             NUMERIC(12, 2),
    positions_json  TEXT
);
"""


def get_conn():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(CREATE_SIGNALS)
        cur.execute(CREATE_TRADES)
        cur.execute(CREATE_PNL)
        conn.commit()
        log.info("Database initialised: %s@%s:%s/%s", DB_USER, DB_HOST, DB_PORT, DB_NAME)
    finally:
        cur.close()
        conn.close()


# ── Signals ────────────────────────────────────────────────────────────────────

def save_signal(signal: dict, user_id: str = "default_single_user", signal_scope: str = "personal") -> int:
    """Insert a signal record, return its ID."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO signals (ts, instrument, signal, confidence, urgency, reason, raw_json, user_id, signal_scope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                datetime.now().isoformat(),
                signal.get("instrument", ""),
                signal.get("signal", ""),
                signal.get("confidence"),
                signal.get("urgency"),
                signal.get("reason"),
                json.dumps(signal),
                user_id,
                signal_scope,
            ),
        )
        signal_id = cur.fetchone()[0]
        conn.commit()
        return signal_id
    finally:
        cur.close()
        conn.close()


def mark_signal_acted(signal_id: int, outcome: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE signals SET acted_on=1, outcome=%s WHERE id=%s",
            (outcome, signal_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def recent_signals(limit: int = 20, user_id: str = "default_single_user") -> list[dict]:
    """Fetch recent signals for a specific user."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM signals WHERE user_id = %s ORDER BY ts DESC LIMIT %s",
            (user_id, limit)
        )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


# ── Trades ─────────────────────────────────────────────────────────────────────

def save_trade(trade: dict, user_id: str = "default_single_user") -> int:
    """Insert a trade record, return its ID."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trades (ts, instrument, txn_type, quantity, price, order_id, signal_id, pnl_realised, notes, user_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                datetime.now().isoformat(),
                trade.get("instrument", ""),
                trade.get("transaction") or trade.get("txn_type", ""),
                trade.get("quantity"),
                trade.get("price"),
                trade.get("order_id"),
                trade.get("signal_id"),
                trade.get("pnl_realised"),
                trade.get("notes"),
                user_id,
            ),
        )
        trade_id = cur.fetchone()[0]
        conn.commit()
        return trade_id
    finally:
        cur.close()
        conn.close()


# ── P&L Snapshots ──────────────────────────────────────────────────────────────

def save_pnl_snapshot(unrealised: float, realised: float, positions: list, user_id: str = "default_single_user") -> None:
    """Save P&L snapshot for a user."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO pnl_snapshots (ts, unrealised, realised, net, positions_json, user_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                datetime.now().isoformat(),
                unrealised,
                realised,
                unrealised + realised,
                json.dumps(positions),
                user_id,
            ),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── Multi-Tenant Helpers ────────────────────────────────────────────────────

def get_user(user_id: str) -> dict | None:
    """Fetch user by user_id."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT user_id, telegram_chat_id, username, broker_type, signal_mode, is_paused FROM users WHERE user_id = %s",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))
    finally:
        cur.close()
        conn.close()


def get_user_by_chat_id(chat_id: int) -> dict | None:
    """Fetch user by Telegram chat_id."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT user_id, telegram_chat_id, username, broker_type, signal_mode, is_paused FROM users WHERE telegram_chat_id = %s",
            (chat_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))
    finally:
        cur.close()
        conn.close()


def get_all_active_users() -> list[dict]:
    """Fetch all non-paused users."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT user_id, telegram_chat_id, username, broker_type, signal_mode, is_paused FROM users WHERE is_paused = FALSE ORDER BY created_at"
        )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


def get_user_signals(user_id: str, signal_scope: str | None = None, limit: int = 20) -> list[dict]:
    """Fetch signals for a user, optionally filtered by scope."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        if signal_scope:
            cur.execute(
                "SELECT * FROM signals WHERE user_id = %s AND signal_scope = %s ORDER BY ts DESC LIMIT %s",
                (user_id, signal_scope, limit)
            )
        else:
            cur.execute(
                "SELECT * FROM signals WHERE user_id = %s ORDER BY ts DESC LIMIT %s",
                (user_id, limit)
            )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


def get_user_trades(user_id: str, limit: int = 20) -> list[dict]:
    """Fetch trades for a user."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM trades WHERE user_id = %s ORDER BY ts DESC LIMIT %s",
            (user_id, limit)
        )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


def get_user_pnl_summary(user_id: str) -> dict | None:
    """Get latest P&L snapshot for a user."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT ts, unrealised, realised, net, positions_json FROM pnl_snapshots
               WHERE user_id = %s ORDER BY ts DESC LIMIT 1""",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))
    finally:
        cur.close()
        conn.close()
