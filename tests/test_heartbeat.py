"""Tests for the liveness heartbeat used by the Docker healthcheck."""
import time

from core import heartbeat as hb


def test_beat_then_fresh(tmp_path):
    p = str(tmp_path / "heartbeat")
    hb.beat(p)
    assert hb.last_beat(p) is not None
    assert hb.age(p) is not None and hb.age(p) < 5
    assert hb.is_fresh(60, p) is True


def test_missing_beat_is_not_fresh(tmp_path):
    p = str(tmp_path / "absent")
    assert hb.last_beat(p) is None
    assert hb.age(p) is None
    assert hb.is_fresh(60, p) is False


def test_stale_beat_detected(tmp_path):
    p = str(tmp_path / "heartbeat")
    hb.beat(p)
    with open(p, "w", encoding="utf-8") as fh:  # backdate to simulate a hung loop
        fh.write(str(time.time() - 1000))
    assert hb.age(p) >= 900
    assert hb.is_fresh(60, p) is False


def test_corrupt_beat_treated_as_missing(tmp_path):
    p = str(tmp_path / "heartbeat")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("not-a-number")
    assert hb.last_beat(p) is None
    assert hb.is_fresh(60, p) is False
