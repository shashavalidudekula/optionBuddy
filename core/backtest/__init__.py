"""Backtest harness: historical data loading/caching and the simulation engine.

The discipline this package enforces: no strategy reaches paper (let alone live)
until it backtests POSITIVE net of realistic costs. The engine reuses the live
cost/slippage model and the go-live gate metrics, so a backtest pass means the
same thing the live gate means.
"""
