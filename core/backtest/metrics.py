"""
metrics.py — the go-live gate, dependency-free (stdlib only).

The PRE-COMMITTED thresholds and the evaluate() that judges a set of closed-trade
records. Lives here (not in scripts/) so BOTH the backtester and
scripts/go_live_readiness import the SAME gate — a backtest PASS means exactly
what a live PASS means. Do NOT relax these to make a struggling strategy pass.
"""
import math
import statistics

# ── PRE-COMMITTED thresholds (change only with explicit sign-off) ─────────────
MIN_TRADES = 100         # round trips — enough that the edge isn't a small-sample fluke
MIN_DAYS = 20            # distinct trading sessions — span multiple days/regimes
MIN_T_STAT = 2.0         # expectancy >0 with ~95% confidence it isn't noise
MIN_PROFIT_FACTOR = 1.3  # net winnings / net losses
MAX_DD_PCT = 15.0        # peak-to-trough net drawdown, as % of start capital


def positions(records: list) -> list:
    """Collapse rows into round trips (group by position_id); oldest→newest by ts."""
    groups: dict = {}
    for i, r in enumerate(records):
        pid = r.get("position_id")
        gid = pid if pid is not None else f"_legacy{i}"
        g = groups.setdefault(gid, {"net": 0.0, "ts": "", "date": ""})
        g["net"] += float(r.get("pnl") or 0)
        ts = str(r.get("ts") or "")
        if ts >= g["ts"]:
            g["ts"], g["date"] = ts, r.get("date") or g["date"]
    return sorted(groups.values(), key=lambda g: g["ts"])


def max_drawdown(nets: list) -> float:
    """Largest peak-to-trough drop of cumulative net P&L (in rupees)."""
    peak = cum = dd = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def evaluate(records: list, start_capital: float) -> tuple:
    """(ready, lines) — gate verdict + printable report over closed-trade records."""
    pos = positions(records)
    nets = [p["net"] for p in pos]
    n = len(nets)
    if n == 0:
        return False, ["No closed trades in the selected window — nothing to evaluate."]

    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    days = len({p["date"] for p in pos if p["date"]})
    total = sum(nets)
    expectancy = total / n
    stdev = statistics.stdev(nets) if n > 1 else 0.0
    t_stat = (expectancy / (stdev / math.sqrt(n))) if stdev > 0 and n > 1 else 0.0
    gross_loss = abs(sum(losses))
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else math.inf
    win_rate = 100.0 * len(wins) / n
    max_dd = max_drawdown(nets)
    max_dd_pct = (max_dd / start_capital * 100) if start_capital else math.inf

    out = [
        f"Round trips      : {n}",
        f"Trading days     : {days}",
        f"Win rate         : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)",
        f"Net P&L          : Rs {total:,.0f}",
        f"Expectancy/trade : Rs {expectancy:,.0f}   (t-stat {t_stat:.2f})",
        f"Profit factor    : {profit_factor:.2f}",
        f"Max drawdown     : Rs {max_dd:,.0f}  ({max_dd_pct:.1f}% of Rs {start_capital:,.0f})",
        "",
    ]
    checks = [
        (f"trades >= {MIN_TRADES}", n >= MIN_TRADES),
        (f"trading days >= {MIN_DAYS}", days >= MIN_DAYS),
        ("expectancy > 0", expectancy > 0),
        (f"t-stat >= {MIN_T_STAT}", t_stat >= MIN_T_STAT),
        (f"profit factor >= {MIN_PROFIT_FACTOR}", profit_factor >= MIN_PROFIT_FACTOR),
        (f"max drawdown <= {MAX_DD_PCT}%", max_dd_pct <= MAX_DD_PCT),
    ]
    ready = all(ok for _, ok in checks)
    for label, ok in checks:
        out.append(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ready, out
