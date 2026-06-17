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
# Compute RSI/EMA/ATR/VWAP/opening-range/tape on OFFICIAL Dhan candles (real-time)
# instead of yfinance (delayed/unofficial). Falls back to yfinance automatically
# when no Dhan session is available or a Dhan history call fails. Set false to
# force yfinance everywhere.
TECHNICALS_USE_DHAN = os.getenv("TECHNICALS_USE_DHAN", "true").lower() == "true"

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
# Master switch: set false to skip Telegram entirely (no init/polling/retry) when
# it's unreachable — e.g. the Indian govt block (to ~22 Jun). Paper trading,
# tracking and the dashboard run unaffected; flip back to true to resume alerts.
TELEGRAM_ENABLED   = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# -- NewsAPI ------------------------------------------------------------------
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")

# -- Risk controls ------------------------------------------------------------
MAX_LOSS_PER_TRADE = int(os.getenv("MAX_LOSS_PER_TRADE", "2000"))
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
# Flat all-in round-trip cost (brokerage + STT + exchange + GST) charged ONCE per
# position when it fully closes, so paper P&L reflects what you'd actually net.
# Realistic intraday option strategies live or die on this number.
PAPER_COST_PER_TRADE    = float(os.getenv("PAPER_COST_PER_TRADE", "100"))
# Adverse slippage per MARKET fill, as a fraction of the option premium. Applied
# to: the entry pay-up (you cross the spread), the protective stop (it fills WORSE
# than its trigger) and every other market exit (EOD/expiry/invalidation/manual).
# Limit/target exits (T1/T2) are NOT slipped — a resting limit fills at its level.
# Modelling zero slippage was the single biggest source of paper-P&L optimism;
# option books are wide, especially near expiry. 0 disables (back to old behaviour).
PAPER_SLIPPAGE_PCT      = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.01"))  # 1% of premium/leg
# Per-trade risk cap (bound the worst-case loss on ANY single trade). A pricey
# BankNifty premium with a wide stop can lose ₹10k on one SL hit, blowing the
# day's budget. Size every position so its worst case (entry→SL) is at most the
# SMALLER of PAPER_RISK_PCT×equity and this absolute rupee cap; if even one lot
# would exceed it, the trade is skipped (logged/flagged 'risk_skip').
PAPER_MAX_LOSS_PER_TRADE = float(os.getenv("PAPER_MAX_LOSS_PER_TRADE", "2000"))
# Master switch for the per-trade risk cap above. false → don't cap/skip on risk;
# size by PAPER_RISK_PCT (floored at PAPER_MIN_LOTS). A single SL can then lose
# more than PAPER_MAX_LOSS_PER_TRADE, but fewer trades get skipped.
PAPER_RISK_CAP_ENABLED  = os.getenv("PAPER_RISK_CAP_ENABLED", "true").lower() == "true"
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

# -- Option-selling margin (paper books B: opt_sell_*) -------------------------
# Selling blocks SPAN+exposure margin, which the long-only paper model never needed.
# These per-lot rupee figures are APPROXIMATIONS for paper sizing only — SEBI/exchange
# margins change and vary intraday; CALIBRATE against Dhan's margin calculator before
# trusting capital adequacy (see GO_LIVE_CRITERIA.md). Defined-risk spreads instead
# block ≈ max-loss (computed from strike width − net credit), far less than naked.
NAKED_MARGIN_PER_LOT = {
    "NIFTY": 110000, "BANKNIFTY": 160000, "FINNIFTY": 110000,
    "MIDCPNIFTY": 90000, "SENSEX": 140000, "BANKEX": 150000,
}
MARGIN_DEFAULT_PER_LOT = float(os.getenv("MARGIN_DEFAULT_PER_LOT", "120000"))  # unknown underlying

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
# EOD close-out schedule (IST, "HH:MM"). At GEN_HALT_TIME new-call generation
# stops and every waiting (untriggered) call is cancelled; at EOD_CLOSE_TIME all
# in-trade calls/positions are squared off; the digest broadcasts at EOD_DIGEST_TIME.
GEN_HALT_TIME   = os.getenv("GEN_HALT_TIME", "15:28")
EOD_CLOSE_TIME  = os.getenv("EOD_CLOSE_TIME", "15:29")
EOD_DIGEST_TIME = os.getenv("EOD_DIGEST_TIME", "15:35")
# Suppress NORMAL (LLM) call generation until this time — lets the deterministic
# opening scalp own the first minutes. Default 09:15 = no suppression.
GEN_RESUME_TIME = os.getenv("GEN_RESUME_TIME", "09:15")

# -- Opening gap scalp (deterministic, no LLM) --------------------------------
# Fade the opening gap: big gap-UP -> buy ATM PE (the follow-through fades);
# big gap-DOWN -> buy ATM CE (the dip bounces). ONE quick trade at the open,
# held a few minutes then closed before the market reconsolidates. Validated on
# 1-min Dhan data — a thin, execution-sensitive edge, so it ships OFF by default;
# enable in paper to measure it (P&L is net of PAPER_COST_PER_TRADE).
OPENING_SCALP_ENABLED = os.getenv("OPENING_SCALP_ENABLED", "false").lower() == "true"
SCALP_UNDERLYING      = os.getenv("SCALP_UNDERLYING", "NIFTY").upper()
SCALP_GAP_MIN_PCT     = float(os.getenv("SCALP_GAP_MIN_PCT", "0.4"))   # min |gap| to act
SCALP_GAPUP_ENTRY_MIN = int(os.getenv("SCALP_GAPUP_ENTRY_MIN", "2"))   # min after open to buy PE
SCALP_GAPDN_ENTRY_MIN = int(os.getenv("SCALP_GAPDN_ENTRY_MIN", "1"))   # min after open to buy CE
SCALP_HOLD_MIN        = int(os.getenv("SCALP_HOLD_MIN", "4"))          # hard exit after N minutes
SCALP_TARGET_PCT      = float(os.getenv("SCALP_TARGET_PCT", "0"))      # premium % target (0 = timer only)
SCALP_STOP_PCT        = float(os.getenv("SCALP_STOP_PCT", "0"))        # premium % stop   (0 = timer only)

# -- Deterministic option selling (opt_sell_spread / opt_sell_naked) -----------
# Generate synthetic short-volatility calls daily (one per underlying). Spreads
# limit max loss; naked shorts have unlimited loss but higher credit. Both routes
# to their own paper books, margin-constrained, with realistic slippage on exits.
SELLING_ENABLED       = os.getenv("SELLING_ENABLED", "true").lower() == "true"  # default ON (paper)
SELLING_UNDERLYINGS   = tuple(
    u.strip() for u in os.getenv("SELLING_UNDERLYINGS", "NIFTY,BANKNIFTY").split(",") if u.strip()
)
SELLING_STRUCTURE     = os.getenv("SELLING_STRUCTURE", "spread").lower()  # "spread" | "naked"
SELLING_DELTA_TARGET  = float(os.getenv("SELLING_DELTA_TARGET", "0.25"))  # pick strike near this delta
SELLING_SPREAD_WIDTH  = int(os.getenv("SELLING_SPREAD_WIDTH", "100"))     # strike width for spreads (points)
SELLING_EXIT_TAKE_PCT = float(os.getenv("SELLING_EXIT_TAKE_PCT", "0.5"))  # take profit at this % of credit
SELLING_EXIT_STOP_MULTIPLE = float(os.getenv("SELLING_EXIT_STOP_MULTIPLE", "2.0"))  # stop at this × credit/width
SELLING_HOLD_DAYS     = int(os.getenv("SELLING_HOLD_DAYS", "7"))          # hard exit after this many days

# -- Equity factor strategy (momentum + low-vol, swing/month) -------------------
# Best evidence-based retail play in India: low-turnover momentum rank + quality
# filter on large-cap equities (NIFTY50 / NIFTYNXT50 or custom universe). Monthly
# rebalance; ~5 concurrent longs. No LLM, no prediction — pure factor exposure.
EQUITY_FACTOR_ENABLED = os.getenv("EQUITY_FACTOR_ENABLED", "true").lower() == "true"  # default ON (paper)
EQUITY_FACTOR_UNIVERSE = tuple(
    u.strip() for u in os.getenv("EQUITY_FACTOR_UNIVERSE", "TCS,INFY,WIPRO,MARUTI,BAJAJFINSV").split(",") if u.strip()
)
EQUITY_FACTOR_REBALANCE_DOW = int(os.getenv("EQUITY_FACTOR_REBALANCE_DOW", "0"))  # day of week (0=Mon)
EQUITY_FACTOR_MIN_MOMENTUM_PCT = float(os.getenv("EQUITY_FACTOR_MIN_MOMENTUM_PCT", "0.0"))  # min 1-month return %
EQUITY_FACTOR_MAX_VOL_PERCENTILE = float(os.getenv("EQUITY_FACTOR_MAX_VOL_PERCENTILE", "60.0"))  # vol <= this %ile

# -- Stock options (illiquid, high caution; buying only) -------------------------
# ATM calls on high-volume liquid stocks. ILLIQUIDITY WARNING: Indian stock option
# spreads are brutal; many don't trade; fills are optimistic. Use only to measure
# and validate against live Dhan data before deploying capital. Buying ONLY.
STOCK_OPT_ENABLED = os.getenv("STOCK_OPT_ENABLED", "true").lower() == "true"  # default ON (paper)
STOCK_OPT_UNDERLYINGS = tuple(
    u.strip() for u in os.getenv("STOCK_OPT_UNDERLYINGS", "TCS,INFY,WIPRO").split(",") if u.strip()
)
STOCK_OPT_MIN_CONFIDENCE = int(os.getenv("STOCK_OPT_MIN_CONFIDENCE", "75"))
STOCK_OPT_MIN_OI = int(os.getenv("STOCK_OPT_MIN_OI", "100"))  # Avoid zero-OI contracts
STOCK_OPT_HOLD_DAYS = int(os.getenv("STOCK_OPT_HOLD_DAYS", "7"))

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

# -- ATR-based target sizing (realistic, volatility-scaled option levels) ------
# Round-number targets ignore today's volatility (BankNifty's "100-pt" gaps).
# Instead derive SL/T1/T2 on the PREMIUM from the underlying's intraday 5-min ATR
# × the strike's delta. Distances are clamped to a sane % of premium so an odd ATR
# can't produce absurd levels; falls back to the model's own levels when data is thin.
ATR_SIZING_ENABLED   = os.getenv("ATR_SIZING_ENABLED", "true").lower() == "true"
ATR_HORIZON_BARS     = int(os.getenv("ATR_HORIZON_BARS", "3"))      # 5-min bars of expected hold move
ATR_SL_MULT          = float(os.getenv("ATR_SL_MULT", "1.0"))
ATR_T1_MULT          = float(os.getenv("ATR_T1_MULT", "1.5"))
ATR_T2_MULT          = float(os.getenv("ATR_T2_MULT", "3.0"))
DEFAULT_OPTION_DELTA = float(os.getenv("DEFAULT_OPTION_DELTA", "0.5"))

# -- Instruments (INDstocks security_ids) ------------------------------------
TRACKED_INDICES    = os.getenv("TRACKED_INDICES", "13,25,1").split(",")      # Nifty50, BankNifty, VIX
TRACKED_STOCKS     = [s.strip() for s in os.getenv("TRACKED_STOCKS", "").split(",") if s.strip()]
TRACKED_COMMODITIES= [c.strip() for c in os.getenv("TRACKED_COMMODITIES", "").split(",") if c.strip()]

# -- Paths --------------------------------------------------------------------
BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH            = os.path.join(BASE_DIR, "data", "trading.db")
LOG_DIR            = os.path.join(BASE_DIR, "logs")
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))  # dated daily logs to keep (0 = forever)

# -- Health / watchdog --------------------------------------------------------
# The main loop writes a heartbeat (epoch seconds) every iteration. scripts/
# healthcheck.py reads it for the Docker HEALTHCHECK; if the loop hangs or the
# process dies, the heartbeat goes stale and the container is marked unhealthy.
# Lives under LOG_DIR (a shared RW volume), so the dashboard can read it too.
HEARTBEAT_PATH      = os.getenv("HEARTBEAT_PATH", os.path.join(LOG_DIR, "heartbeat"))
HEARTBEAT_STALE_SEC = int(os.getenv("HEARTBEAT_STALE_SEC", "60"))  # > this ⇒ unhealthy
# Feed-stale alert: during the active polling window, if no index quotes come back
# for this long, the owner gets ONE Telegram alert (and a recovery note) — because
# a blind loop can't track stops or square off. 0 / false disables.
FEED_ALERTS_ENABLED = os.getenv("FEED_ALERTS_ENABLED", "true").lower() == "true"
FEED_STALE_SEC      = int(os.getenv("FEED_STALE_SEC", "120"))

# Position reconciliation (LIVE only): periodically compare internal open positions
# against the broker's actual book and alert the owner on any mismatch (a rejected /
# partial fill leaves the two out of sync). Read-only — never places orders. No-op
# in paper mode (there is no broker book to compare against).
RECONCILE_ENABLED      = os.getenv("RECONCILE_ENABLED", "true").lower() == "true"
RECONCILE_INTERVAL_SEC = int(os.getenv("RECONCILE_INTERVAL_SEC", "60"))
