# Multi-Tenant OptionBuddy Deployment Guide

This guide covers deploying the multi-tenant F&O trading agent (Phases 1-5) to production.

## Architecture Overview

```
┌─────────────────────────────────────┐
│   FastAPI Web Dashboard (Port 8000) │
│   - User registration & login       │
│   - Broker credential management    │
│   - Telegram linking                │
│   - User settings & P&L dashboard   │
└──────────────┬──────────────────────┘
               │
       ┌───────▼──────────┐
       │  PostgreSQL      │
       │  Multi-tenant    │
       │  Database        │
       └───────┬──────────┘
               │
┌──────────────▼──────────────────────┐
│  Multi-Tenant Trading Agent         │
│  (Single Process, Multiple Users)   │
│  - Per-user signal generation       │
│  - Multi-broker support (3 brokers) │
│  - Market-wide shared signals       │
│  - Daily briefings & news updates   │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┐
│  Telegram Bot (Single Instance)      │
│  - Per-user message routing          │
│  - Signal approval/rejection         │
│  - P&L queries & commands            │
└──────────────────────────────────────┘
```

## Deployment Steps

### 1. Prerequisites

- Docker & Docker Compose
- PostgreSQL 16+
- Telegram Bot (created via @BotFather)
- Broker API credentials for test users
- Gemini API key for signal generation
- NewsAPI key for news fetching

### 2. Environment Variables

Create a `.env` file in the project root:

```env
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_BOT_USERNAME=@your_bot_username

# Gemini AI
GEMINI_API_KEY=your_gemini_key_here
GEMINI_MODEL=gemini-1.5-flash

# NewsAPI
NEWSAPI_KEY=your_newsapi_key_here

# Database (use docker-compose defaults)
DB_HOST=postgres
DB_PORT=5432
DB_NAME=trading_agent
DB_USER=trading_user
DB_PASSWORD=trading_password

# Web Dashboard
WEB_HOST=http://localhost:8000
ADMIN_API_KEY=your_admin_key_here

# Security
ENCRYPTION_KEY=your_32_byte_base64_key_here  # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# INDstocks API (if using INDstocks broker)
INDSTOCKS_BASE_URL=https://api.indstocks.com/v1
INDSTOCKS_LOGIN_URL=https://auth.indstocks.com

# Redis (optional, for future caching)
REDIS_URL=redis://redis:6379
```

**Generate ENCRYPTION_KEY:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Database Setup

The system uses PostgreSQL with multi-tenant schema. Migration is automated on startup:

```bash
# Manual migration (if needed)
python data/migrate_to_multitenant.py --migrate

# Rollback
python data/migrate_to_multitenant.py --rollback
```

**Key tables:**
- `users` - User accounts with encrypted broker credentials
- `broker_accounts` - Multi-broker support per user
- `signals` - All signals with user_id & signal_scope ('personal' or 'shared')
- `trades` - Executed trades per user
- `pnl_snapshots` - Daily P&L snapshots per user

### 4. Broker Integration

Supported brokers:

| Broker | Adapter | API | Tested |
|--------|---------|-----|--------|
| INDstocks | IndStocksAdapter | REST + WebSocket | ✓ Yes |
| Zerodha | ZerodhaAdapter | KiteConnect (REST) | ⚠️ In Progress |
| Grow | GrowAdapter | REST | ⚠️ In Progress |

Each user connects their own broker account via the web dashboard.

### 5. Docker Deployment

#### Option A: Local Development

```bash
# Start services
docker-compose up

# Stop
docker-compose down

# View logs
docker-compose logs -f trading-agent
docker-compose logs -f postgres
```

#### Option B: AWS EC2 (t3.small recommended)

```bash
# 1. SSH into EC2 instance
ssh -i your-key.pem ubuntu@your-ec2-ip

# 2. Install Docker & Docker Compose
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker

# 3. Clone repository
git clone https://github.com/your-org/OptionBuddy.git
cd OptionBuddy

# 4. Create .env file with your keys
nano .env  # Paste environment variables

# 5. Switch to feature/multi-tenant branch
git checkout feature/multi-tenant

# 6. Build and start
docker-compose up -d

# 7. Verify
docker-compose ps
docker-compose logs -f trading-agent
```

#### Option C: Kubernetes (Future)

For scaling to 100+ users, migrate to Kubernetes:

```yaml
# kubernetes/deployment.yaml (example)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: trading-agent
spec:
  replicas: 2
  selector:
    matchLabels:
      app: trading-agent
  template:
    metadata:
      labels:
        app: trading-agent
    spec:
      containers:
      - name: trading-agent
        image: optionbuddy:latest
        env:
        - name: DB_HOST
          value: "postgres.default.svc.cluster.local"
```

### 6. Web Dashboard Access

After deployment:

1. **Register**: `http://your-server:8000/register`
2. **Login**: `http://your-server:8000/login`
3. **Connect Broker**: Select broker type, enter API credentials
4. **Link Telegram**: Click link → start bot → `/verify <user_id>`
5. **Dashboard**: View P&L, positions, settings

### 7. Testing the Multi-Tenant System

#### Test 1: Multiple User Registration

```bash
# In browser or via curl
curl -X POST http://localhost:8000/register \
  -d "username=friend1&password=pass123&email=friend1@example.com"

curl -X POST http://localhost:8000/register \
  -d "username=friend2&password=pass123&email=friend2@example.com"

# Verify users in database
docker-compose exec postgres psql -U trading_user -d trading_agent -c \
  "SELECT user_id, username, broker_type, signal_mode FROM users;"
```

#### Test 2: Broker Connections

Each user connects their own broker:

```
Friend 1: INDstocks account
Friend 2: Zerodha account
Friend 3: Grow account
```

Verify via dashboard → /dashboard/{user_id}

#### Test 3: Telegram Linking

1. Start bot: `/start <user_id>`
2. Verify: `/verify <user_id>`
3. Check: `/status` should show broker type
4. Test P&L: `/pnl` (only shows YOUR P&L, not others)

#### Test 4: Personal & Shared Signals

1. User 1 (Personal mode): Receives signals from their own positions only
2. User 2 (Shared mode): Receives only market-wide signals
3. User 3 (Both mode): Receives both personal and shared signals

**Verify in database:**
```sql
SELECT user_id, signal_scope, confidence FROM signals 
WHERE created_at > NOW() - INTERVAL '1 hour';
```

#### Test 5: Data Isolation

Verify User A cannot see User B's P&L:

```bash
# User A queries P&L
curl http://localhost:8000/dashboard/user_a_id

# Should NOT show User B's P&L
# Check database - signals have user_id FK
docker-compose exec postgres psql -U trading_user -d trading_agent -c \
  "SELECT DISTINCT user_id, COUNT(*) FROM signals GROUP BY user_id;"
```

### 8. Monitoring & Maintenance

#### Logs

```bash
# Trading agent logs
docker-compose logs -f trading-agent

# Telegram bot activity
docker-compose logs trading-agent | grep "telegram\|Signal alert"

# Database logs
docker-compose logs postgres
```

#### Health Checks

```bash
# Database connectivity
docker-compose exec postgres pg_isready -U trading_user

# Telegram bot polling
docker-compose logs trading-agent | grep "polling started"

# Active users
docker-compose exec postgres psql -U trading_user -d trading_agent -c \
  "SELECT COUNT(*) FROM users WHERE is_paused = FALSE;"
```

#### Backup & Restore

```bash
# Backup
docker-compose exec postgres pg_dump -U trading_user trading_agent > backup.sql

# Restore
docker-compose exec postgres psql -U trading_user trading_agent < backup.sql
```

### 9. Performance Tuning

**Current Capacity:** t3.small (~8GB RAM, 2 vCPU) handles 20-30 concurrent users

**Bottlenecks:**
- Database queries (add indexes on user_id, created_at)
- API rate limits (Gemini 1500 calls/day, NewsAPI 100 calls/day)
- Telegram polling (1000 chats per bot)

**Optimization:**
1. Add Redis caching for market data
2. Use async database drivers (asyncpg)
3. Implement rate limiting per user
4. Batch Telegram messages

### 10. Troubleshooting

#### Issue: "Database connection refused"
```bash
docker-compose ps
docker-compose logs postgres
# Ensure postgres is healthy before trading-agent starts
docker-compose restart postgres
```

#### Issue: Telegram bot not receiving messages
```bash
# Check bot token
docker-compose logs trading-agent | grep "polling started"
# Verify TELEGRAM_BOT_TOKEN in .env is correct
```

#### Issue: User signals not being generated
```bash
# Check user has open positions
docker-compose logs trading-agent | grep "user_id" | grep "positions"
# Verify user's broker credentials are valid
# Check Gemini API quota
```

#### Issue: User A seeing User B's P&L
```bash
# Data isolation bug - check signals table
docker-compose exec postgres psql -U trading_user -d trading_agent -c \
  "SELECT user_id, COUNT(*) FROM signals GROUP BY user_id HAVING COUNT(*) > 100;"
# Should show even distribution across users, no user has excess signals
```

## Phase Completion Status

| Phase | Component | Status | Files |
|-------|-----------|--------|-------|
| 1 | Database Migration | ✓ Complete | data/migrate_to_multitenant.py |
| 2 | Broker Abstraction | ✓ Complete | core/brokers/*.py |
| 3 | Web Dashboard | ✓ Complete | web/main.py, web/secrets.py |
| 4 | Multi-Tenant Agent | ✓ Complete | main_multitenant.py, core/user_context.py |
| 5 | Shared Signals | ✓ Complete | signals/shared_signals.py |

## Next Steps (Post-MVP)

1. **Kubernetes Deployment**: Scale to 100+ users
2. **Mobile App**: React Native dashboard
3. **Strategy Templates**: Pre-configured signal modes (conservative, aggressive)
4. **Leaderboard**: Top traders by returns (anonymized)
5. **Risk Limits**: Per-broker daily loss limits
6. **Multi-Account**: One user → multiple brokers
7. **Webhook Notifications**: Discord, Slack, Email
8. **Advanced Analytics**: XIRR, drawdown analysis, win rate

## Support

- **Issues**: Raise on GitHub with logs and error details
- **Database**: Use pgAdmin4 container for debugging
- **Telegram**: Use BotFather debug mode for message inspection

---

**Deployment Date:** [Your Date]  
**Last Updated:** May 31, 2026  
**Version:** 1.0 (Multi-Tenant MVP)
