"""heartbeat.py — liveness signal for the advisory loop.

The orchestrator calls `beat()` once per loop iteration to stamp "I'm alive" into a
small file. `scripts/healthcheck.py` reads it for the Docker HEALTHCHECK: if the
loop hangs or the process dies, the stamp goes stale and the container is reported
unhealthy (so an orchestrator / autoheal sidecar can restart it). The file lives in
LOG_DIR, a shared read-write volume, so the dashboard can surface it too.

Writes are atomic (write-temp + os.replace) so a healthcheck never reads a
half-written stamp.
"""
import os
import time

from config.settings import HEARTBEAT_PATH


def beat(path: str | None = None) -> None:
    """Stamp the current epoch seconds into the heartbeat file (atomic)."""
    path = path or HEARTBEAT_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(time.time()))
    os.replace(tmp, path)  # atomic on POSIX and Windows


def last_beat(path: str | None = None) -> float | None:
    """Epoch seconds of the last beat, or None if missing/unreadable."""
    path = path or HEARTBEAT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def age(path: str | None = None) -> float | None:
    """Seconds since the last beat, or None if there is no readable beat yet."""
    lb = last_beat(path)
    return None if lb is None else max(0.0, time.time() - lb)


def is_fresh(max_age: float, path: str | None = None) -> bool:
    """True when a beat exists and is no older than `max_age` seconds."""
    a = age(path)
    return a is not None and a <= max_age
