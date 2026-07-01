"""healthcheck.py — Docker HEALTHCHECK probe for the advisory loop.

Exits 0 when the heartbeat is fresh, 1 when it's missing or stale (loop hung /
process dead). Wired in docker-compose.yml on the advisory-agent service.

    python scripts/healthcheck.py
"""
import os
import sys

# Allow `python scripts/healthcheck.py` (Docker HEALTHCHECK) as well as
# `python -m scripts.healthcheck` — put the repo root on the path either way.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import HEARTBEAT_STALE_SEC  # noqa: E402
from core.heartbeat import age  # noqa: E402


def main() -> int:
    a = age()
    if a is None:
        print("heartbeat: missing")
        return 1
    if a > HEARTBEAT_STALE_SEC:
        print(f"heartbeat: stale ({a:.0f}s > {HEARTBEAT_STALE_SEC}s)")
        return 1
    print(f"heartbeat: ok ({a:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
