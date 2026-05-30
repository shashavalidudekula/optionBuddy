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

def save_signal(signal: dict) -> int:
    """Insert a signal record, return its ID."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO signals (ts, instrument, signal, confidence, urgency, reason, raw_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                datetime.now().isoformat(),
                signal.get("instrument", ""),
                signal.get("signal", ""),
                signal.get("confidence"),
                signal.get("urgency"),
                signal.get("reason"),
                json.dumps(signal),
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


def recent_signals(limit: int = 20) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT %s", (limit,)
        )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


# ── Trades ─────────────────────────────────────────────────────────────────────

def save_trade(trade: dict) -> int:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trades (ts, instrument, txn_type, quantity, price, order_id, signal_id, pnl_realised, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ),
        )
        trade_id = cur.fetchone()[0]
        conn.commit()
        return trade_id
    finally:
        cur.close()
        conn.close()


# ── P&L Snapshots ──────────────────────────────────────────────────────────────

def save_pnl_snapshot(unrealised: float, realised: float, positions: list) -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO pnl_snapshots (ts, unrealised, realised, net, positions_json)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                datetime.now().isoformat(),
                unrealised,
                realised,
                unrealised + realised,
                json.dumps(positions),
            ),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
