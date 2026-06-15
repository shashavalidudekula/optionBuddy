"""
settings.py -- All configuration loaded from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# -- Market-data provider -----------------------------------------------------
# Which live feed powers the snapshot, option chains and price tracking:
#   "dhan"      → DhanHQ v2 (native greeks/IV/OI; default — we trade on Dhan)
#   "indstocks" → INDstocks (legacy fallback; LTP + instruments only)
# Everything goes through core/market_data_provider.py, so dropping INDstocks
# later is: delete core/indstocks_*.py + remove the one branch in that facade.
MARKET_DATA_PROVIDER = os.getenv("MARKET_DATA_PROVIDER", "dhan").lower()

# -- Dhan (DhanHQ v2) ---------------------------------------------------------
# Auth = client-id + access-token headers. The access token is regenerated daily;
# with an API key + TOTP it can be auto-refreshed (see core/dhan_auth.py).
DHAN_CLIENT_ID     = os.getenv("DHAN_CLIENT_ID", "")
DHAN_BASE_URL      = os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2")
# Auto token generation (preferred): with TOTP enabled on the Dhan account, the
# daily access token is generated headlessly from client-id + PIN + TOTP secret
# (no manual copy). Secrets stay in .env (gitignored); the token lives in memory
# only — never logged in full, never written to disk. See core/dhan_auth.py.
DHAN_PIN           = os.getenv("DHAN_PIN", "")
DHAN_TOTP_SECRET   = os.getenv("DHAN_TOTP_SECRET", "")
DHAN_AUTH_BASE_URL = os.getenv("DHAN_AUTH_BASE_URL", "https://auth.dhan.co")
# Optional static token — used only as a fallback when auto-generation isn't
# configured (i.e. DHAN_PIN / DHAN_TOTP_SECRET are blank).
DHAN_ACCESS_TOKEN  = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_SCRIP_MASTER_URL = os.getenv(
    "DHAN_SCRIP_MASTER_URL", "https://images.dhan.co/api-data/api-scrip-master.csv")
# Underlying → (security_id, segment) for the option-chain endpoint. Dhan indices
# live in the IDX_I segment. Equity/future underlyings are resolved from the scrip
# master at runtime, so only indices need seeding here. Verify these ids against
# the scrip master for your account before going live.
DHAN_INDEX_UNDERLYINGS = {
    "NIFTY":      (13, "IDX_I"),
    "BANKNIFTY":  (25, "IDX_I"),
    "FINNIFTY":   (27, "IDX_I"),
    "MIDCPNIFTY": (442, "IDX_I"),
    "SENSEX":     (51, "IDX_I"),
    "BANKEX":     (69, "IDX_I"),
    "INDIAVIX":   (21, "IDX_I"),
}

# -- INDstocks (legacy fallback — see MARKET_DATA_PROVIDER) --------------------
INDSTOCKS_ACCESS_TOKEN = os.getenv("INDSTOCKS_ACCESS_TOKEN", "")
INDSTOCKS_BASE_URL     = os.getenv("INDSTOCKS_BASE_URL", "https://api.indstocks.com")

# -- Execution mode -----------------------------------------------------------
# "paper" → simulated only (zero real money). "live" → route real Dhan orders.
# DOUBLE GUARD: live orders are sent ONLY when EXECUTION_MODE=live AND
# DHAN_ALLOW_LIVE_ORDERS=true. With the mode flipped but the guard off, the
# live broker stays inert (logs + no-ops) so you can dry-run the wiring safely.
EXECUTION_MODE        = os.getenv("EXECUTION_MODE", "paper").lower()
DHAN_ALLOW_LIVE_ORDERS = os.getenv("DHAN_ALLOW_LIVE_ORDERS", "false").lower() == "true"

# -- LLM provider -------------------------------------------------------------
# Which backend powers call generation + position review: "azure" | "openai" | "gemini".
LLM_PROVIDER       = os.getenv("LLM_PROVIDER", "azure").lower()

# Gemini (Google AI Studio) — legacy/fallback.
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Azure OpenAI — endpoint like https://<resource>.openai.azure.com/ ; DEPLOYMENT is the
# deployment NAME you gave the model (e.g. "gpt-4.1-mini"), not the base model id.
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
# Optional separate deployment for the heavier /review reasoning (e.g. gpt-4o).
# Falls back to AZURE_OPENAI_DEPLOYMENT when unset. Generation uses the default
# (point it at a cheaper mini model for the high-frequency call generation).
AZURE_OPENAI_DEPLOYMENT_REVIEW = os.getenv("AZURE_OPENAI_DEPLOYMENT_REVIEW", "")

# OpenAI direct (if LLM_PROVIDER="openai").
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# -- Telegram -----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# -- NewsAPI ------------------------------------------------------------------
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")

# -- Risk controls ------------------------------------------------------------
MAX_LOSS_PER_TRADE = int(os.getenv("MAX_LOSS_PER_TRADE", "2000"))
DAILY_LOSS_LIMIT   = int(os.getenv("DAILY_LOSS_LIMIT", "5000"))
MAX_LOTS_PER_ORDER = int(os.getenv("MAX_LOTS_PER_ORDER", "2"))
AUTO_EXECUTE       = os.getenv("AUTO_EXECUTE", "false").lower() == "true"
MIN_CONFIDENCE     = int(os.getenv("MIN_CONFIDENCE", "75"))

# -- Paper trading (shadow mode) ----------------------------------------------
# A simulated account that "takes" qualifying calls with zero real money, so the
# engine's edge can be proven before any live execution is ever considered.
PAPER_TRADING_ENABLED   = os.getenv("PAPER_TRADING_ENABLED", "true").lower() == "true"
PAPER_START_CAPITAL     = float(os.getenv("PAPER_START_CAPITAL", "100000"))
PAPER_RISK_PCT          = float(os.getenv("PAPER_RISK_PCT", "0.02"))   # risk 2% of equity to stop
PAPER_MIN_LOTS          = int(os.getenv("PAPER_MIN_LOTS", "2"))         # floor: every trade is >= this many lots
# Ceiling on lots per trade. Risk-sizing divides by the per-unit stop distance, so
# a cheap near-expiry option (premium ₹1-25) explodes into 20-300 lots of lottery
# tickets unless capped (0 = uncapped).
PAPER_MAX_LOTS          = int(os.getenv("PAPER_MAX_LOTS", "10"))
PAPER_MAX_OPEN          = int(os.getenv("PAPER_MAX_OPEN", "0"))         # max concurrent positions (0 = unlimited; capital is the only limit)
PAPER_DAILY_LOSS_PCT    = float(os.getenv("PAPER_DAILY_LOSS_PCT", "0.04"))  # halt new entries for the day
PAPER_PARTIAL_FRACTION  = float(os.getenv("PAPER_PARTIAL_FRACTION", "0.6"))  # book this much at T1 (60%); hold 40% for T2
# Flat all-in round-trip cost (brokerage + STT + exchange + GST + slippage proxy)
# charged ONCE per position when it fully closes, so paper P&L reflects what you'd
# actually net. Realistic intraday option strategies live or die on this number.
PAPER_COST_PER_TRADE    = float(os.getenv("PAPER_COST_PER_TRADE", "100"))
# Categories the paper trader will act on. Scope: long options + stocks only —
# NO futures and NO short positions (those need a margin model; out of scope).
PAPER_CATEGORIES        = tuple(
    c.strip() for c in os.getenv("PAPER_CATEGORIES", "index_option,equity").split(",") if c.strip()
)
# One-time destructive reset of the paper account on startup (wipes positions/
# fills, restores PAPER_START_CAPITAL). Set true for one boot, then back to false.
PAPER_RESET_ON_START    = os.getenv("PAPER_RESET_ON_START", "false").lower() == "true"
# Weekly fresh start: on the first run of each new ISO week (Monday), wipe the
# paper account back to PAPER_START_CAPITAL. Closed-trade history in
# logs/paper_history.jsonl is untouched, so the dashboard's weekly/daily P&L
# tables keep the full record. Set false to let capital compound across weeks.
PAPER_WEEKLY_RESET      = os.getenv("PAPER_WEEKLY_RESET", "true").lower() == "true"
# Fallback lot sizes when the instruments master is unavailable (SEBI revises these
# periodically; the master is preferred when a session is present).
PAPER_LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65,
    "MIDCPNIFTY": 120, "SENSEX": 20, "BANKEX": 30,
}

# -- Timing -------------------------------------------------------------------
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "5"))   # fast loop: tracking + trigger checks
MARKET_OPEN        = os.getenv("MARKET_OPEN", "09:15")
MARKET_CLOSE       = os.getenv("MARKET_CLOSE", "15:30")

# -- End-of-day square-off ----------------------------------------------------
# When true, NOTHING carries overnight: at EOD every open paper position is
# closed at the day's last price and every still-active call (triggered or not)
# is force-closed — so each trading day starts flat with fresh calls only.
# When false, the old behaviour applies: only options expiring today are settled
# and only unfilled entries are cancelled; in-trade calls on still-valid
# contracts carry to the next day.
EOD_SQUARE_OFF_ALL = os.getenv("EOD_SQUARE_OFF_ALL", "true").lower() == "true"

# -- Live generation engine ---------------------------------------------------
# index_option calls are event-driven: regenerate when the market actually moves,
# not on a fixed timer. Guard rails keep the LLM spend sane.
OPT_GEN_MIN_GAP_SEC   = int(os.getenv("OPT_GEN_MIN_GAP_SEC", "45"))    # cooldown between option scans
OPT_GEN_FLOOR_SEC     = int(os.getenv("OPT_GEN_FLOOR_SEC", "120"))     # scan at least this often
GEN_MOVE_PCT          = float(os.getenv("GEN_MOVE_PCT", "0.15"))       # index % move since last scan
GEN_VIX_JUMP_PCT      = float(os.getenv("GEN_VIX_JUMP_PCT", "3.0"))    # India VIX % change since last scan
# Non-option categories (equity/futures/commodity) are swing-y — slower cadence.
OTHER_GEN_INTERVAL_MIN = int(os.getenv("OTHER_GEN_INTERVAL_MIN", "15"))
# ATM strike step per index, for the "new ATM strike" trigger.
ATM_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}

# -- Tape-alignment gate (don't fight the intraday trend) ---------------------
# Reactive guard against "PUTs into a rally". Uses realized intraday momentum
# (day move, 30-min momentum, 5-min EMA trend), never a prediction.
#   Layer 2 (entry):  block new option/futures calls that fight a clear trend.
#   Layer 3 (exit):   cut an open position when the underlying decisively reverses.
TAPE_FILTER_ENABLED = os.getenv("TAPE_FILTER_ENABLED", "true").lower() == "true"
TAPE_MIN_MOVE_PCT   = float(os.getenv("TAPE_MIN_MOVE_PCT", "0.25"))   # day move to call a trend (entry)
TAPE_EXIT_ENABLED   = os.getenv("TAPE_EXIT_ENABLED", "true").lower() == "true"
TAPE_EXIT_MOVE_PCT  = float(os.getenv("TAPE_EXIT_MOVE_PCT", "0.40"))  # stronger move to cut a held loser
# Reversal off the day's extremes. The from-open rules above are blind to an
# afternoon slide that starts from a big morning gain (+1.2% → +0.5% still reads
# "up" all the way down). A pullback of this size from the day high/low, with
# momentum agreeing, overrides the from-open read.
TAPE_REVERSAL_PCT      = float(os.getenv("TAPE_REVERSAL_PCT", "0.35"))   # pullback to call a reversal (entry gate)
TAPE_EXIT_REVERSAL_PCT = float(os.getenv("TAPE_EXIT_REVERSAL_PCT", "0.50"))  # stronger pullback to cut a held position

# -- Post-T1 stop trail ---------------------------------------------------------
# Once T1 is hit, the stop trails to lock this fraction of the entry→T1 move
# (0 = old breakeven behaviour, 1 = stop exactly at T1). 0.75 → SL sits 25% of
# the move below T1, so the runner keeps most of the T1 profit instead of riding
# all the way back to flat.
T1_TRAIL_LOCK_FRACTION = float(os.getenv("T1_TRAIL_LOCK_FRACTION", "0.75"))

# -- Profit protection (don't let a winning trade turn into a loss) ------------
# Wide targets (e.g. BankNifty T1 ~100 pts) often see price run most of the way,
# then reverse to SL — a winner becomes a full loss. These act BEFORE T1:
#   1. Breakeven+ ratchet: once price covers PROFIT_LOCK_FRACTION of the entry→T1
#      distance, trail the SL to lock PROFIT_TRAIL_KEEP of the favorable move
#      (never below breakeven) — a trade in real profit can't close red.
#   2. Time-in-profit partial: if price holds >= PROFIT_STALL_FRACTION of the way
#      to T1 for PROFIT_STALL_MINUTES without reaching T1, book a partial and lock
#      the SL — bank some, hold the rest.
PROFIT_PROTECT_ENABLED = os.getenv("PROFIT_PROTECT_ENABLED", "true").lower() == "true"
PROFIT_LOCK_FRACTION   = float(os.getenv("PROFIT_LOCK_FRACTION", "0.5"))   # frac of entry→T1 to start locking
PROFIT_TRAIL_KEEP      = float(os.getenv("PROFIT_TRAIL_KEEP", "0.5"))      # frac of the favorable move locked into SL
PROFIT_STALL_FRACTION  = float(os.getenv("PROFIT_STALL_FRACTION", "0.3"))  # min frac toward T1 to count as "in profit"
PROFIT_STALL_MINUTES   = int(os.getenv("PROFIT_STALL_MINUTES", "10"))      # in profit this long but no T1 → book partial

# -- Instruments (INDstocks security_ids) ------------------------------------
TRACKED_INDICES    = os.getenv("TRACKED_INDICES", "13,25,1").split(",")      # Nifty50, BankNifty, VIX
TRACKED_STOCKS     = [s.strip() for s in os.getenv("TRACKED_STOCKS", "").split(",") if s.strip()]
TRACKED_COMMODITIES= [c.strip() for c in os.getenv("TRACKED_COMMODITIES", "").split(",") if c.strip()]

# -- Paths --------------------------------------------------------------------
BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH            = os.path.join(BASE_DIR, "data", "trading.db")
LOG_DIR            = os.path.join(BASE_DIR, "logs")
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))  # dated daily logs to keep (0 = forever)
