"""
indstocks_auth.py -- INDstocks API session

Auth is a JWT access token from the INDstocks dashboard.
NOTE: the token is short-lived — it expires roughly daily (around 07:00 IST),
so it must be refreshed each trading day (re-copy it and recreate the container).

Get your token:
  1. Login to indstocks.com
  2. Go to API section -> "Get Started with INDstocks APIs"
  3. Copy your access token -> set INDSTOCKS_ACCESS_TOKEN in .env

Run standalone: python -m core.indstocks_auth
"""
import requests

from config.settings import INDSTOCKS_ACCESS_TOKEN, INDSTOCKS_BASE_URL
from config.logger import get_logger

log = get_logger("indstocks_auth")


class IndStocksSession:
    """Thin wrapper around requests.Session with auth header pre-set."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": INDSTOCKS_ACCESS_TOKEN,
            "Content-Type": "application/json",
        })
        self.base_url = INDSTOCKS_BASE_URL.rstrip("/")

    def get(self, path, **kwargs):
        resp = self.session.get(self.base_url + path, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post(self, path, json=None, **kwargs):
        resp = self.session.post(self.base_url + path, json=json, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def verify(self):
        """Check token is valid by fetching profile."""
        data = self.get("/user/profile")
        return data.get("status") == "success"


def get_session() -> IndStocksSession:
    s = IndStocksSession()
    if not s.verify():
        raise ValueError("INDstocks token invalid. Check INDSTOCKS_ACCESS_TOKEN in .env")
    log.info("INDstocks session verified.")
    return s


if __name__ == "__main__":
    s = get_session()
    pos = s.get("/portfolio/positions", params={"segment": "derivative", "product": "margin"})
    # API may return {"data": {"net_positions": [...]}} or {"data": [...]} or []
    if isinstance(pos, list):
        net = pos
    else:
        data = pos.get("data", [])
        if isinstance(data, dict):
            net = data.get("net_positions", [])
        elif isinstance(data, list):
            net = data
        else:
            net = []
    # Filter to active positions only (same logic as PositionTracker.fetch)
    open_pos = [p for p in net if int(p.get("net_quantity", 0)) != 0 and p.get("position_type") == "open"]
    print("All positions returned by API:", len(net))
    print("Active (non-zero qty, open):", len(open_pos))
    for p in open_pos:
        print(" ", p.get("trading_symbol"), "| qty:", p.get("net_quantity"), "| pnl:", p.get("pnl_absolute"))
