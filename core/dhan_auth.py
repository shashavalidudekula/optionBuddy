"""
dhan_auth.py — DhanHQ v2 API session.

Auth is two headers on every request:
    access-token : the daily access token from the Dhan web platform
    client-id    : your Dhan client id

NOTE: the access token is regenerated daily. With an API key + TOTP configured
on your Dhan account you can auto-regenerate it via API (build that refresh here
later); for now it is read from DHAN_ACCESS_TOKEN and re-copied each trading day.

Get your credentials:
  1. Login to web.dhan.co
  2. DhanHQ Trading APIs → generate / copy the access token (and note client id)
  3. set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env

Data APIs (quotes / option chain) require the Dhan Data API subscription
(₹499+tax/mo, or free with ≥25 trades in the last 30 days).

Run standalone: python -m core.dhan_auth
"""
import requests

from config.settings import DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_BASE_URL
from config.logger import get_logger

log = get_logger("dhan_auth")


class DhanSession:
    """Thin wrapper around requests.Session with Dhan auth headers pre-set."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "access-token": DHAN_ACCESS_TOKEN,
            "client-id": DHAN_CLIENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.base_url = DHAN_BASE_URL.rstrip("/")

    def get(self, path, **kwargs):
        resp = self.session.get(self.base_url + path, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post(self, path, json=None, **kwargs):
        resp = self.session.post(self.base_url + path, json=json, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def verify(self):
        """Validate token + data-API access by fetching NIFTY's expiry list."""
        try:
            data = self.post(
                "/optionchain/expirylist",
                json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
            )
            return data.get("status") == "success"
        except Exception as e:  # noqa: BLE001
            log.warning("Dhan verify failed: %s", e)
            return False


def get_session() -> DhanSession:
    if not (DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN):
        raise ValueError("Dhan not configured — set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env")
    s = DhanSession()
    if not s.verify():
        raise ValueError(
            "Dhan token invalid or Data API not enabled. Check DHAN_ACCESS_TOKEN/"
            "DHAN_CLIENT_ID and that the Data API subscription is active."
        )
    log.info("Dhan session verified.")
    return s


if __name__ == "__main__":
    s = get_session()
    chain = s.post("/optionchain", json={
        "UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I",
        "Expiry": s.post("/optionchain/expirylist",
                         json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"})["data"][0],
    })
    oc = chain.get("data", {}).get("oc", {})
    print("NIFTY spot:", chain.get("data", {}).get("last_price"))
    print("Strikes returned:", len(oc))
