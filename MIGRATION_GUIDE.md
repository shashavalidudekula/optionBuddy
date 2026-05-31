# Migration Guide: Single-User → Multi-Tenant System

This guide explains how to migrate from the original single-user trading agent to the multi-tenant system.

## Key Differences

### Single-User (main.py)
- One user account stored in `.env`
- Direct broker credentials in environment
- All signals from one person
- Single Telegram chat

### Multi-Tenant (main_multitenant.py)
- Multiple users with database accounts
- Encrypted credentials in PostgreSQL
- Per-user signal generation and routing
- Multiple Telegram chats (one per user)
- Web dashboard for onboarding

## Migration Path

### Phase 1: Backup Existing Data

```bash
# Export your current P&L & trades
sqlite3 signals.db "SELECT * FROM trades;" > backup_trades.csv
sqlite3 signals.db "SELECT * FROM pnl_snapshots;" > backup_pnl.csv

# Or if using PostgreSQL already
pg_dump trading_agent > backup_single_user.sql
```

### Phase 2: Database Migration

The database schema evolves from single-user to multi-tenant:

#### Before (Single-User)
```sql
CREATE TABLE signals (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    instrument TEXT,
    signal TEXT,
    confidence INT,
    reason TEXT,
    -- No user_id column
);

CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    instrument TEXT,
    quantity INT,
    entry_price FLOAT,
    -- No user_id column
);
```

#### After (Multi-Tenant)
```sql
-- New users table
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    telegram_chat_id BIGINT UNIQUE,
    username TEXT UNIQUE,
    password_hash TEXT,
    broker_type TEXT,          -- 'indstocks', 'zerodha', 'grow'
    broker_credentials BYTEA,  -- Encrypted
    signal_mode TEXT,          -- 'personal', 'shared', 'both'
    is_paused BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Updated with user_id
ALTER TABLE signals ADD COLUMN user_id TEXT REFERENCES users(user_id);
ALTER TABLE signals ADD COLUMN signal_scope TEXT DEFAULT 'personal';
ALTER TABLE trades ADD COLUMN user_id TEXT REFERENCES users(user_id);
ALTER TABLE pnl_snapshots ADD COLUMN user_id TEXT REFERENCES users(user_id);

CREATE INDEX idx_signals_user_ts ON signals(user_id, ts DESC);
CREATE INDEX idx_trades_user_ts ON trades(user_id, ts DESC);
```

**Run automatic migration:**
```bash
python data/migrate_to_multitenant.py --migrate
```

**Manual migration:**
```sql
-- 1. Create new users table
\i migrations/001_create_users.sql

-- 2. Add user_id columns
\i migrations/002_add_user_id_columns.sql

-- 3. Create broker_accounts table
\i migrations/003_create_broker_accounts.sql

-- 4. Backfill with default user (for backward compatibility)
INSERT INTO users (user_id, username, broker_type, signal_mode, is_paused)
VALUES ('default_single_user', 'existing_user', 'indstocks', 'personal', FALSE);

UPDATE signals SET user_id = 'default_single_user' WHERE user_id IS NULL;
UPDATE trades SET user_id = 'default_single_user' WHERE user_id IS NULL;
```

### Phase 3: Credential Migration

#### Before: Credentials in .env
```env
INDSTOCKS_API_KEY=your_key
INDSTOCKS_API_SECRET=your_secret
```

#### After: Encrypted in Database
```python
# Encrypt and store in database
from web.secrets import encrypt_json

creds = {
    'api_key': 'your_key',
    'api_secret': 'your_secret'
}
encrypted = encrypt_json(creds)

# Store in users.broker_credentials (BYTEA)
cursor.execute(
    "UPDATE users SET broker_credentials = %s WHERE user_id = %s",
    (encrypted, 'default_single_user')
)
```

**Migration script:**
```python
import os
from web.secrets import encrypt_json
from data.store import get_conn

db = get_conn()
cursor = db.cursor()

# Get existing user
cursor.execute("SELECT user_id FROM users WHERE username = 'existing_user'")
user_id = cursor.fetchone()[0]

# Encrypt credentials from .env
creds = {
    'api_key': os.getenv('INDSTOCKS_API_KEY'),
    'api_secret': os.getenv('INDSTOCKS_API_SECRET')
}
encrypted = encrypt_json(creds)

# Update database
cursor.execute(
    "UPDATE users SET broker_credentials = %s WHERE user_id = %s",
    (encrypted, user_id)
)
db.commit()
cursor.close()
db.close()

print(f"Migrated credentials for {user_id}")
```

### Phase 4: Configuration Changes

#### Dockerfile

**Before:**
```dockerfile
CMD ["python", "main.py"]
```

**After:**
```dockerfile
CMD ["python", "main_multitenant.py"]
```

#### docker-compose.yml

**Add web service for dashboard:**
```yaml
web:
  build: .
  container_name: optionbuddy-web
  restart: unless-stopped
  command: uvicorn web.main:app --host 0.0.0.0 --port 8000
  depends_on:
    - postgres
  env_file:
    - .env
  environment:
    DB_HOST: postgres
    DB_PORT: 5432
  ports:
    - "8000:8000"
  networks:
    - trading-network
```

#### Environment Variables

**Add for multi-tenant:**
```env
# New encryption key for credential storage
ENCRYPTION_KEY=your_32_byte_base64_key

# Web dashboard
WEB_HOST=http://localhost:8000
ADMIN_API_KEY=admin_key_here

# Telegram bot username (for /start links)
TELEGRAM_BOT_USERNAME=your_bot_username
```

### Phase 5: Code Changes

#### Import Changes

**Before:**
```python
from notifier.telegram_bot import TelegramBot
bot = TelegramBot()
await bot.send_message(text)
```

**After:**
```python
from notifier.telegram_bot_multitenant import TelegramBotMultitenant
bot = TelegramBotMultitenant()
await bot.send_user_message(chat_id, text)  # Per-user chat_id
```

#### User Context

**Before:**
```python
# Single global positions
positions = get_positions()
signal = get_signal(positions, market, headlines)
```

**After:**
```python
# Per-user context
for user_id, ctx in users.items():
    positions = await ctx.fetch_positions()
    signal = get_signal(positions, market, headlines, user_id=user_id)
```

#### Signal Saving

**Before:**
```python
save_signal(signal, urgency="high", confidence=75)
```

**After:**
```python
save_signal(
    signal,
    user_id=user_id,              # New: track which user
    signal_scope='personal'        # New: 'personal' or 'shared'
)
```

### Phase 6: Testing

#### Test 1: Backward Compatibility

The system should still work with the default single user:

```bash
# Migrate database
python data/migrate_to_multitenant.py --migrate

# Start trading agent
python main_multitenant.py

# Check logs for "Loaded 1 active users" (the default user)
```

#### Test 2: Multi-User Onboarding

```bash
# 1. Start web dashboard
uvicorn web.main:app --reload

# 2. Register users
curl -X POST http://localhost:8000/register \
  -d "username=friend1&password=pass123&email=friend1@example.com"

# 3. Verify in database
psql -d trading_agent -c "SELECT username, broker_type FROM users;"

# 4. Connect broker credentials
# Via web UI: /connect-broker

# 5. Link Telegram
# Via web UI: /link-telegram
# Or in Telegram: /start user_id → /verify user_id

# 6. Check trading agent loaded users
docker-compose logs trading-agent | grep "Loaded.*active users"
```

#### Test 3: P&L Isolation

```bash
# Create 2 test users
INSERT INTO users (user_id, username, broker_type, signal_mode, is_paused, telegram_chat_id)
VALUES ('user1', 'friend1', 'indstocks', 'personal', FALSE, 123456789);

INSERT INTO users (user_id, username, broker_type, signal_mode, is_paused, telegram_chat_id)
VALUES ('user2', 'friend2', 'indstocks', 'personal', FALSE, 987654321);

-- User1's P&L
SELECT SUM(unrealised) FROM pnl_snapshots WHERE user_id = 'user1';

-- User2's P&L (different, isolated)
SELECT SUM(unrealised) FROM pnl_snapshots WHERE user_id = 'user2';
```

### Phase 7: Rollback Plan

If you need to go back to single-user:

```bash
# Stop multi-tenant system
docker-compose down

# Rollback database
python data/migrate_to_multitenant.py --rollback

# Or restore from backup
psql -d trading_agent < backup_single_user.sql

# Switch back to main.py
git checkout main

# Restart single-user
python main.py
```

## Files to Update

| File | Change | Impact |
|------|--------|--------|
| Dockerfile | CMD: main.py → main_multitenant.py | Trading agent |
| docker-compose.yml | Add web service | Dashboard access |
| .env | Add ENCRYPTION_KEY, WEB_HOST | Security, web config |
| main.py | DEPRECATED | Keep for reference |
| main_multitenant.py | NEW (use this) | Multi-user support |
| telegram_bot.py | DEPRECATED | Use telegram_bot_multitenant.py |
| data/store.py | Updated with user_id params | Database queries |
| signals/claude_engine.py | No changes | Still used by main_multitenant.py |
| signals/shared_signals.py | NEW | Market-wide signals |

## Database Schema Version

```
Version 1.0 (Single-User)
    ↓ migrate_to_multitenant.py
Version 2.0 (Multi-Tenant)
```

Track version in `schema_version` table:
```sql
CREATE TABLE schema_version (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO schema_version VALUES ('2.0');
```

## Troubleshooting Migration

### Issue: "Duplicate key value violates unique constraint"

**Cause:** Username or chat_id already exists from old data

**Solution:**
```sql
-- Check for duplicates
SELECT username, COUNT(*) FROM users GROUP BY username HAVING COUNT(*) > 1;

-- Delete one of the duplicates
DELETE FROM users WHERE user_id = 'old_id' AND username = 'duplicate';
```

### Issue: "Foreign key constraint failed"

**Cause:** signal/trade has user_id that doesn't exist in users table

**Solution:**
```sql
-- Find orphaned signals
SELECT * FROM signals WHERE user_id NOT IN (SELECT user_id FROM users);

-- Delete orphaned or reassign to default user
UPDATE signals SET user_id = 'default_single_user' 
WHERE user_id NOT IN (SELECT user_id FROM users);
```

### Issue: Credentials won't decrypt

**Cause:** ENCRYPTION_KEY changed or missing

**Solution:**
```bash
# Generate consistent encryption key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Add to .env
ENCRYPTION_KEY=your_generated_key

# Restart containers
docker-compose restart
```

## Success Criteria

After migration, verify:

- ✓ Database migrated to multi-tenant schema
- ✓ Existing user data preserved (default_single_user)
- ✓ New users can register via web dashboard
- ✓ Each user's P&L is isolated
- ✓ Telegram bot routes messages per user
- ✓ Multi-broker support works (INDstocks, Zerodha, Grow)
- ✓ Shared signals broadcast to all users
- ✓ Performance acceptable on t3.small (< 5% CPU)

## Timeline

- **Phase 1-2**: 1-2 hours (backup, database migration)
- **Phase 3-4**: 30 min (credentials, configuration)
- **Phase 5-6**: 1 hour (code changes, testing)
- **Total**: ~3-4 hours

## Support

If migration fails:
1. Check logs: `docker-compose logs postgres`
2. Restore backup: `psql < backup_single_user.sql`
3. Verify .env variables
4. Open GitHub issue with error message & logs
