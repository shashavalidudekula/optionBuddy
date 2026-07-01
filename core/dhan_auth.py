"""
dhan_auth.py — DhanHQ v2 API session with secure auto token generation.

Every API call sends two headers: `client-id` and `access-token`.

ACCESS TOKEN — two modes:
  1. Auto (preferred, with TOTP enabled on the account): the daily token is
     generated headlessly from client-id + PIN + TOTP secret via
     POST https://auth.dhan.co/app/generateAccessToken. It is refreshed
     automatically before expiry and on any 401. No manual daily copy.
  2. Static fallback: if DHAN_PIN / DHAN_TOTP_SECRET are not set, a manually
     pasted DHAN_ACCESS_TOKEN is used.

SECURITY — the token is treated as a secret end to end:
  • Generated and held IN MEMORY only — never written to disk or any cache file.
  • NEVER logged in full — only a short masked fingerprint (first 6 / last 4).
  • Secrets (PIN, TOTP secret) live solely in .env, which is gitignored.
  • All auth calls go over HTTPS to auth.dhan.co.
Keep DHAN_PIN and DHAN_TOTP_SECRET out of source control, screenshots and logs.

Get set up:
  1. web.dhan.co → DhanHQ Trading APIs → create API key, enable TOTP (save the
     base32 secret from the QR), note your client id and login PIN.
  2. set DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET in .env.

Run standalone: python -m core.dhan_auth   (prints a masked token + NIFTY spot)
"""
import threading
from datetime import datetime, timedelta

import requests

from config.settings import (
    DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_BASE_URL,
    DHAN_PIN, DHAN_TOTP_SECRET, DHAN_AUTH_BASE_URL,
)
from config.logger import get_logger

log = get_logger("dhan_auth")

# Refresh this far ahead of the stated expiry so a long-running process never
# makes a request with an about-to-die token.
_REFRESH_SKEW = timedelta(minutes=10)


def _auto_enabled() -> bool:
    return bool(DHAN_CLIENT_ID and DHAN_PIN and DHAN_TOTP_SECRET)


def _mask(token: str | None) -> str:
    """A non-reversible fingerprint for logs — never the full token."""
    if not token:
        return "<none>"
    return f"{token[:6]}…{token[-4:]} (len {len(token)})" if len(token) > 12 else "<set>"


def _parse_expiry(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _generate_token() -> tuple[str, datetime | None]:
    """Generate a fresh 24h access token via TOTP. Returns (token, expiry).

    Computes a fresh 6-digit TOTP from the shared secret at call time. The PIN
    and TOTP secret never leave this process; they go straight to Dhan over TLS.
    """
    import pyotp  # local import so the dep is only needed when auto-token is used

    code = pyotp.TOTP(DHAN_TOTP_SECRET).now()
    params = {"dhanClientId": DHAN_CLIENT_ID, "totp": code}
    if DHAN_PIN:
        params["pin"] = DHAN_PIN
    url = f"{DHAN_AUTH_BASE_URL.rstrip('/')}/app/generateAccessToken"

    # The endpoint is documented as POST; fall back to GET if the verb 404/405s.
    resp = None
    for method in ("post", "get"):
        resp = getattr(requests, method)(url, params=params, timeout=15)
        if resp.status_code not in (404, 405):
            break
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    token = (data.get("accessToken") or data.get("access_token")
             or inner.get("accessToken") or inner.get("access_token"))
    if not token:
        # Do NOT include the response body — it may echo sensitive fields.
        raise ValueError(f"Dhan token generation returned no accessToken (status={data.get('status')})")
    expiry = _parse_expiry(data.get("expiryTime") or data.get("expiry_time")
                           or inner.get("expiryTime"))
    return token, expiry


class DhanSession:
    """requests.Session wrapper that keeps a valid access token in memory."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "client-id": DHAN_CLIENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.base_url = DHAN_BASE_URL.rstrip("/")
        self._expiry: datetime | None = None
        self._lock = threading.Lock()
        self._bootstrap()

    # ── token lifecycle ──────────────────────────────────────────────────────
    def _bootstrap(self) -> None:
        if _auto_enabled():
            self._refresh(force=True)
        elif DHAN_ACCESS_TOKEN:
            self.session.headers["access-token"] = DHAN_ACCESS_TOKEN
            log.info("Dhan using static access token (%s).", _mask(DHAN_ACCESS_TOKEN))
        else:
            raise ValueError(
                "Dhan not configured — set DHAN_PIN + DHAN_TOTP_SECRET (auto token) "
                "or DHAN_ACCESS_TOKEN (manual) in .env."
            )

    def _refresh(self, force: bool = False) -> None:
        if not _auto_enabled():
            return
        with self._lock:
            # Re-check under lock so concurrent threads don't regenerate twice.
            if not force and self._expiry and datetime.now() < self._expiry - _REFRESH_SKEW:
                return
            token, expiry = _generate_token()
            self.session.headers["access-token"] = token  # in-memory header only
            self._expiry = expiry
            log.info("Dhan access token refreshed (expires %s, %s)",
                     expiry.isoformat() if expiry else "?", _mask(token))

    def _ensure_token(self) -> None:
        if _auto_enabled() and self._expiry and datetime.now() >= self._expiry - _REFRESH_SKEW:
            self._refresh()

    # ── requests ─────────────────────────────────────────────────────────────
    def get(self, path, **kwargs):
        self._ensure_token()
        return self._request("get", self.base_url + path, **kwargs)

    def post(self, path, json=None, **kwargs):
        self._ensure_token()
        return self._request("post", self.base_url + path, json=json, **kwargs)

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 10)
        resp = getattr(self.session, method)(url, **kwargs)
        if resp.status_code == 401 and _auto_enabled():
            log.warning("Dhan 401 — regenerating access token and retrying once.")
            self._refresh(force=True)
            resp = getattr(self.session, method)(url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def verify(self) -> bool:
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
    if not DHAN_CLIENT_ID:
        raise ValueError("Dhan not configured — set DHAN_CLIENT_ID in .env")
    s = DhanSession()
    if not s.verify():
        raise ValueError(
            "Dhan token invalid or Data API not enabled. Check credentials and that "
            "the Data API subscription is active."
        )
    log.info("Dhan session verified (%s).",
             "auto-TOTP token" if _auto_enabled() else "static token")
    return s


if __name__ == "__main__":
    # Smoke test — prints only a MASKED token, never the real value.
    s = get_session()
    chain = s.post("/optionchain", json={
        "UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I",
        "Expiry": s.post("/optionchain/expirylist",
                         json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"})["data"][0],
    })
    print("Auth mode  :", "auto-TOTP" if _auto_enabled() else "static")
    print("Token      :", _mask(s.session.headers.get("access-token")))
    print("NIFTY spot :", chain.get("data", {}).get("last_price"))
    print("Strikes    :", len(chain.get("data", {}).get("oc", {})))
