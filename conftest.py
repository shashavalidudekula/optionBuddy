"""Pytest bootstrap for the trading-agent unit tests.

Keeps the suite runnable on any machine — including CI / dev boxes that don't have
the Postgres driver or a live .env — by:

  1. Putting the repo root on sys.path so `config` / `core` / `data` / `signals`
     import as top-level packages regardless of pytest's import mode.
  2. Stubbing `psycopg2` ONLY when it isn't installed. The unit tests exercise pure
     money-math (slippage, P&L, sizing, stop-trailing) and never open a connection,
     but a few modules do `import psycopg2` at module load. On the real runtime box
     (where the driver is present) no stub is inserted, so integration tests are
     unaffected.
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if importlib.util.find_spec("psycopg2") is None:
    sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))
