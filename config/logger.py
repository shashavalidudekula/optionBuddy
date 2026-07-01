"""
logger.py — Daily date-stamped file logger + console output

Each day's log lives in its own file (agent-YYYY-MM-DD.log). The handler
switches files when the date changes — no renaming, so it's safe with the
multiple handler instances this module creates (one per named logger) and
on Windows, where renaming an open file fails. Files older than
LOG_RETENTION_DAYS are purged on startup and at each day switch.
"""
import glob
import logging
import os
from datetime import datetime, timedelta
from config.settings import LOG_DIR, LOG_RETENTION_DAYS

os.makedirs(LOG_DIR, exist_ok=True)


def _purge_old_logs() -> None:
    """Delete agent-YYYY-MM-DD.log files older than LOG_RETENTION_DAYS."""
    if LOG_RETENTION_DAYS <= 0:  # 0 = keep forever
        return
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    for path in glob.glob(os.path.join(LOG_DIR, "agent-*.log")):
        stamp = os.path.basename(path)[len("agent-"):-len(".log")]
        if stamp < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass  # in use or already gone — retry at the next day switch


class DailyFileHandler(logging.FileHandler):
    """Appends to agent-<today>.log, moving to a fresh file when the date changes."""

    def __init__(self, log_dir: str):
        self._log_dir = log_dir
        self._day = datetime.now().strftime("%Y-%m-%d")
        super().__init__(self._path(), encoding="utf-8")

    def _path(self) -> str:
        return os.path.join(self._log_dir, f"agent-{self._day}.log")

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self.close()  # FileHandler reopens lazily on the next emit
            self.baseFilename = os.path.abspath(self._path())
            _purge_old_logs()
        super().emit(record)


_purge_old_logs()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # One date-stamped file per day
    fh = DailyFileHandler(LOG_DIR)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
