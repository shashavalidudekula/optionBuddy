"""
migrate_to_multitenant.py — Database migration for multi-tenant support

Migrates from single-user schema to multi-tenant schema while preserving existing data.

Tables modified:
  - signals: Add user_id, signal_scope
  - trades: Add user_id
  - pnl_snapshots: Add user_id

Tables created:
  - users: Multi-user accounts with broker credentials
  - broker_accounts: Support for multiple brokers per user (future)

Usage:
  python -m data.migrate_to_multitenant --migrate
  python -m data.migrate_to_multitenant --rollback
"""

import psycopg2
import os
import sys
import uuid
from datetime import datetime
from config.logger import get_logger

log = get_logger("migrate")

# Database connection parameters
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "trading_agent")
DB_USER = os.getenv("DB_USER", "trading_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "trading_password")


def get_conn():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    return conn


# ============================================================================
# NEW TABLES (for multi-tenant)
# ============================================================================

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    telegram_chat_id BIGINT UNIQUE,
    username TEXT UNIQUE,
    password_hash TEXT,
    broker_type TEXT,
    broker_credentials BYTEA,
    daily_loss_limit NUMERIC(12, 2) DEFAULT 50000,
    signal_mode TEXT DEFAULT 'personal',
    is_paused BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_BROKER_ACCOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS broker_accounts (
    account_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    broker_type TEXT NOT NULL,
    api_key TEXT NOT NULL,
    api_secret BYTEA NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# ============================================================================
# MIGRATION UP (add columns + indexes + create new tables)
# ============================================================================

def migrate_up():
    """Apply multi-tenant migration."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        log.info("Starting migration to multi-tenant schema...")

        # Step 1: Create new users table
        log.info("Creating users table...")
        cur.execute(CREATE_USERS_TABLE)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_chat_id ON users(telegram_chat_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_broker ON users(broker_type)")

        # Step 2: Create broker_accounts table
        log.info("Creating broker_accounts table...")
        cur.execute(CREATE_BROKER_ACCOUNTS_TABLE)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user ON broker_accounts(user_id)")

        # Step 3: Add user_id to signals table
        log.info("Adding user_id to signals table...")
        cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS user_id TEXT")
        cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_scope TEXT DEFAULT 'personal'")

        # Step 4: Add user_id to trades table
        log.info("Adding user_id to trades table...")
        cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS user_id TEXT")

        # Step 5: Add user_id to pnl_snapshots table
        log.info("Adding user_id to pnl_snapshots table...")
        cur.execute("ALTER TABLE pnl_snapshots ADD COLUMN IF NOT EXISTS user_id TEXT")

        # Step 6: Create indexes
        log.info("Creating indexes...")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_user_ts ON signals(user_id, ts DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_user_scope ON signals(user_id, signal_scope)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, ts DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pnl_user_ts ON pnl_snapshots(user_id, ts DESC)")

        # Step 7: Create default user for backward compatibility
        log.info("Creating default user for backward compatibility...")
        default_user_id = "default_single_user"
        try:
            cur.execute("""
                INSERT INTO users (user_id, username, broker_type, signal_mode)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (default_user_id, "default_user", "indstocks", "personal"))
        except Exception as e:
            log.warning(f"Could not create default user: {e}")

        # Step 8: Backfill existing data with default user_id
        log.info("Backfilling existing data with default user_id...")
        cur.execute("UPDATE signals SET user_id = %s WHERE user_id IS NULL", (default_user_id,))
        cur.execute("UPDATE trades SET user_id = %s WHERE user_id IS NULL", (default_user_id,))
        cur.execute("UPDATE pnl_snapshots SET user_id = %s WHERE user_id IS NULL", (default_user_id,))

        # Step 9: Add NOT NULL constraints after backfill
        log.info("Adding NOT NULL constraints...")
        cur.execute("ALTER TABLE signals ALTER COLUMN user_id SET NOT NULL")
        cur.execute("ALTER TABLE trades ALTER COLUMN user_id SET NOT NULL")
        cur.execute("ALTER TABLE pnl_snapshots ALTER COLUMN user_id SET NOT NULL")

        # Step 10: Add foreign key constraints
        log.info("Adding foreign key constraints...")
        try:
            cur.execute("""
                ALTER TABLE signals
                ADD CONSTRAINT fk_signals_user_id
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            """)
        except psycopg2.Error:
            log.info("Foreign key constraint on signals already exists")

        try:
            cur.execute("""
                ALTER TABLE trades
                ADD CONSTRAINT fk_trades_user_id
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            """)
        except psycopg2.Error:
            log.info("Foreign key constraint on trades already exists")

        try:
            cur.execute("""
                ALTER TABLE pnl_snapshots
                ADD CONSTRAINT fk_pnl_user_id
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            """)
        except psycopg2.Error:
            log.info("Foreign key constraint on pnl_snapshots already exists")

        conn.commit()
        log.info("✅ Migration UP completed successfully!")

    except Exception as e:
        conn.rollback()
        log.error(f"❌ Migration failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================================
# MIGRATION DOWN (rollback)
# ============================================================================

def migrate_down():
    """Rollback multi-tenant migration."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        log.info("Starting rollback to single-user schema...")

        # Step 1: Drop foreign key constraints
        log.info("Dropping foreign key constraints...")
        try:
            cur.execute("ALTER TABLE signals DROP CONSTRAINT fk_signals_user_id")
        except psycopg2.Error:
            pass

        try:
            cur.execute("ALTER TABLE trades DROP CONSTRAINT fk_trades_user_id")
        except psycopg2.Error:
            pass

        try:
            cur.execute("ALTER TABLE pnl_snapshots DROP CONSTRAINT fk_pnl_user_id")
        except psycopg2.Error:
            pass

        # Step 2: Drop indexes
        log.info("Dropping indexes...")
        cur.execute("DROP INDEX IF EXISTS idx_signals_user_ts")
        cur.execute("DROP INDEX IF EXISTS idx_signals_user_scope")
        cur.execute("DROP INDEX IF EXISTS idx_trades_user_ts")
        cur.execute("DROP INDEX IF EXISTS idx_pnl_user_ts")
        cur.execute("DROP INDEX IF EXISTS idx_accounts_user")
        cur.execute("DROP INDEX IF EXISTS idx_users_chat_id")
        cur.execute("DROP INDEX IF EXISTS idx_users_broker")

        # Step 3: Drop columns
        log.info("Dropping user_id columns...")
        cur.execute("ALTER TABLE signals DROP COLUMN IF EXISTS user_id")
        cur.execute("ALTER TABLE signals DROP COLUMN IF EXISTS signal_scope")
        cur.execute("ALTER TABLE trades DROP COLUMN IF EXISTS user_id")
        cur.execute("ALTER TABLE pnl_snapshots DROP COLUMN IF EXISTS user_id")

        # Step 4: Drop new tables
        log.info("Dropping new tables...")
        cur.execute("DROP TABLE IF EXISTS broker_accounts")
        cur.execute("DROP TABLE IF EXISTS users")

        conn.commit()
        log.info("✅ Migration DOWN completed successfully!")

    except Exception as e:
        conn.rollback()
        log.error(f"❌ Rollback failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_to_multitenant.py [--migrate|--rollback]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "--migrate":
        migrate_up()
    elif action == "--rollback":
        confirm = input("⚠️  This will DROP the users table and remove all user_id columns. Continue? (yes/no): ")
        if confirm.lower() == "yes":
            migrate_down()
        else:
            print("Rollback cancelled")
    else:
        print(f"Unknown action: {action}")
        print("Usage: python migrate_to_multitenant.py [--migrate|--rollback]")
        sys.exit(1)
