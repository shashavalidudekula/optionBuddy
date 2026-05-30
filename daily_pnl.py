#!/usr/bin/env python3
"""
daily_pnl.py -- Show today's closed trades with P&L in table format

Run: docker exec optionbuddy-trading-agent python daily_pnl.py
"""
from core.indstocks_auth import get_session
from datetime import datetime
from tabulate import tabulate

session = get_session()

# Fetch all positions
resp = session.get("/portfolio/positions", params={"segment": "derivative", "product": "margin"})

if isinstance(resp, list):
    positions = resp
else:
    data = resp.get("data", [])
    if isinstance(data, dict):
        positions = data.get("net_positions", [])
    elif isinstance(data, list):
        positions = data
    else:
        positions = []

# Filter for closed trades (net_qty == 0) and build table data
trades = []
total_pnl = 0
total_trades = 0

for p in positions:
    net_qty = int(p.get("net_qty", 0))

    # Show closed positions (net_qty == 0)
    if net_qty == 0:
        symbol = p.get("symbol", "")
        strike = p.get("drv_strike_price", "")
        opt_type = p.get("drv_option_type", "")

        # Build full symbol
        if strike and opt_type:
            full_symbol = f"{symbol}{strike}{opt_type}"
        else:
            full_symbol = symbol

        buy_qty = int(p.get("buy_qty", 0))
        sell_qty = int(p.get("sell_qty", 0))
        buy_avg = float(p.get("buy_avg", 0))
        sell_avg = float(p.get("sell_avg", 0))
        realized_pnl = float(p.get("realized_profit", 0))

        # Determine direction
        if buy_qty > sell_qty:
            direction = "LONG"
            qty = buy_qty - sell_qty
        elif sell_qty > buy_qty:
            direction = "SHORT"
            qty = sell_qty - buy_qty
        else:
            direction = "CLOSED"
            qty = max(buy_qty, sell_qty)

        expiry = p.get("drv_expiry_date", "")

        trades.append([
            full_symbol,
            direction,
            qty,
            f"{buy_avg:.2f}",
            f"{sell_avg:.2f}",
            f"{realized_pnl:,.2f}",
            "✓" if realized_pnl > 0 else "✗"
        ])

        total_pnl += realized_pnl
        total_trades += 1

# Display table
if trades:
    headers = ["Symbol", "Direction", "Qty", "Avg Buy", "Avg Sell", "P&L (₹)", "Win"]
    print("\n" + "="*90)
    print(f"TODAY'S CLOSED TRADES - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*90)
    print(tabulate(trades, headers=headers, tablefmt="grid"))
    print("="*90)
    print(f"Total Trades: {total_trades}")
    print(f"Total P&L: ₹ {total_pnl:,.2f}")
    print(f"Win Rate: {sum(1 for t in trades if float(t[5].replace(',', '')) > 0)}/{total_trades}")
    print("="*90 + "\n")
else:
    print("\nNo closed trades found.\n")

# Also show open positions (net_qty != 0)
open_positions = []
for p in positions:
    net_qty = int(p.get("net_qty", 0))
    if net_qty != 0:
        symbol = p.get("symbol", "")
        strike = p.get("drv_strike_price", "")
        opt_type = p.get("drv_option_type", "")

        if strike and opt_type:
            full_symbol = f"{symbol}{strike}{opt_type}"
        else:
            full_symbol = symbol

        direction = "LONG" if net_qty > 0 else "SHORT"
        avg_price = float(p.get("avg_price", 0))
        unrealised_pnl = float(p.get("unrealised_pnl", 0))

        open_positions.append([
            full_symbol,
            direction,
            abs(net_qty),
            f"{avg_price:.2f}",
            f"{unrealised_pnl:,.2f}"
        ])

if open_positions:
    headers = ["Symbol", "Direction", "Qty", "Avg Price", "Unrealised P&L (₹)"]
    print("\nOPEN POSITIONS")
    print("="*70)
    print(tabulate(open_positions, headers=headers, tablefmt="grid"))
    print("="*70 + "\n")
else:
    print("\nNo open positions.\n")
