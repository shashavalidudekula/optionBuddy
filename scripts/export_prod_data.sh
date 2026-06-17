#!/usr/bin/env bash
# export_prod_data.sh — pull the artifacts needed to validate OptionBuddy's edge.
#
# READ-ONLY: SELECT queries, file copies, and non-secret env only. Places everything
# in ./prod_data/. Run it on the PROD host (where `docker compose` is up), then copy
# the prod_data/ folder back to the analysis machine.
#
#   bash scripts/export_prod_data.sh
#   PG=optionbuddy-postgres APP=optionbuddy-advisory bash scripts/export_prod_data.sh
#
# Secrets (DHAN_*, *_KEY, *_TOKEN, *_SECRET, *_PIN, *_PASSWORD) are never exported.

set -uo pipefail

PG="${PG:-optionbuddy-postgres}"
APP="${APP:-optionbuddy-advisory}"
DB_USER="${DB_USER:-trading_user}"
DB_NAME="${DB_NAME:-trading_agent}"
OUT="${OUT:-prod_data}"

mkdir -p "$OUT"
echo "Exporting to $OUT/  (pg=$PG  app=$APP  db=$DB_NAME)"

pg()   { docker exec "$PG" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
step() { echo "  -> $1"; }
rows() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }

# 1. Durable realized-P&L ledger (the must-have) + decision log + agent logs.
step "paper_history.jsonl"
docker cp "$APP:/app/logs/paper_history.jsonl" "$OUT/paper_history.jsonl" 2>/dev/null \
  && echo "     ok ($(rows "$OUT/paper_history.jsonl") rows)" || echo "     MISSING (no paper trades yet?)"

step "call_decisions.jsonl"
docker cp "$APP:/app/logs/call_decisions.jsonl" "$OUT/call_decisions.jsonl" 2>/dev/null \
  && echo "     ok" || echo "     skipped"

step "agent logs (rolling + dated)"
docker cp "$APP:/app/logs/agent.log" "$OUT/agent.log" 2>/dev/null && echo "     agent.log"
docker exec "$APP" sh -c 'ls /app/logs/agent-*.log 2>/dev/null' 2>/dev/null | while read -r f; do
  [ -n "$f" ] && docker cp "$APP:$f" "$OUT/$(basename "$f")" 2>/dev/null && echo "     $(basename "$f")"
done

# 2. calls table -> CSV (full signal population + outcomes).
step "calls.csv"
pg -c "COPY (SELECT id,category,instrument,underlying,action,entry_price,entry_min,entry_max,target_1,target_2,stop_loss,confidence,status,entry_triggered,last_price,result_pct,paper_status,issued_at,entry_triggered_at,target1_hit_at,closed_at,option_expiry FROM calls ORDER BY id) TO STDOUT WITH CSV HEADER" > "$OUT/calls.csv" 2>/dev/null \
  && echo "     ok ($(rows "$OUT/calls.csv") lines incl header)"
if [ ! -s "$OUT/calls.csv" ]; then
  step "calls.csv (SELECT * fallback — column mismatch)"
  pg -c "COPY (SELECT * FROM calls ORDER BY id) TO STDOUT WITH CSV HEADER" > "$OUT/calls.csv" \
    && echo "     ok ($(rows "$OUT/calls.csv") lines)" || echo "     FAILED"
fi

# 3. paper_fills -> CSV (per-leg fills: entry/exit/kind/price).
step "paper_fills.csv"
pg -c "COPY (SELECT * FROM paper_fills ORDER BY ts) TO STDOUT WITH CSV HEADER" > "$OUT/paper_fills.csv" 2>/dev/null \
  && echo "     ok ($(rows "$OUT/paper_fills.csv") lines)" || echo "     FAILED"

# 4. paper_account snapshot (capital / peak / drawdown).
step "paper_account.csv"
pg -c "COPY (SELECT * FROM paper_account) TO STDOUT WITH CSV HEADER" > "$OUT/paper_account.csv" 2>/dev/null \
  && echo "     ok" || echo "     skipped"

# 5. Non-secret strategy flags (the edge is config-dependent). Secrets excluded.
step "config_flags.txt"
docker exec "$APP" printenv 2>/dev/null \
  | grep -iE '^(TAPE_|PROFIT_|ATR_|PAPER_|GEN_|OPT_GEN_|OPENING_SCALP|SCALP_|ADVISORY_CATEGORIES|MIN_CONFIDENCE|MARKET_OPEN|MARKET_CLOSE|EOD_|MARKET_DATA_PROVIDER|LLM_PROVIDER|EXECUTION_MODE|DHAN_ALLOW_LIVE_ORDERS|FEED_|RECONCILE_|POLL_INTERVAL|TECHNICALS_)' \
  | grep -ivE 'KEY|TOKEN|SECRET|PIN|PASSWORD' \
  | sort > "$OUT/config_flags.txt" \
  && echo "     ok ($(rows "$OUT/config_flags.txt") flags)" || echo "     skipped"

echo
echo "Done. Summary:"
echo "  paper_history rows : $(rows "$OUT/paper_history.jsonl")"
echo "  calls lines        : $(rows "$OUT/calls.csv")"
echo "  paper_fills lines  : $(rows "$OUT/paper_fills.csv")"
echo
echo "Copy the whole $OUT/ folder back to the analysis machine."
