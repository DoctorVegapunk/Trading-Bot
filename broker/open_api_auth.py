"""
cTrader Open API OAuth 2.0 helper.

Handles the authorization code flow and automatic token refresh.

CLI usage (initial setup):
    python -m broker.open_api_auth

This prints the authorization URL, waits for you to paste the auth code,
exchanges it for tokens, and saves them to secrets/open_api_token.json.

After that, the bot handles token refresh automatically (every ~25 days).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote

log = logging.getLogger(__name__)

AUTH_HOST = "https://id.ctrader.com"
TOKEN_HOST = "https://openapi.ctrader.com"
TOKEN_PATH = "/apps/token"
AUTH_PATH = "/my/settings/openapi/grantingaccess/"
REDIRECT_URI = "http://localhost:8080"
TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "secrets",
    "open_api_token.json",
)


class OpenApiAuth:
    """OAuth 2.0 for cTrader Open API — token lifecycle management."""

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ValueError("OPEN_API_CLIENT_ID and OPEN_API_CLIENT_SECRET must be set")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[dict[str, Any]] = None

    # ── Public API ────────────────────────────────────────────────────────

    @staticmethod
    def get_auth_url(client_id: str, redirect_uri: str = REDIRECT_URI, scope: str = "trading") -> str:
        """Generate the authorization URL for the user to visit."""
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "product": "web",
        })
        return f"{AUTH_HOST}{AUTH_PATH}?{params}"

    def get_valid_token(self, refresh_token: Optional[str] = None) -> str:
        """
        Return a valid access token.

        Priority:
          1. In-memory cached token (if still fresh)
          2. OPEN_API_TOKEN_JSON env var (Secret Manager fallback)
          3. Token file on disk (if still fresh)
          4. Refresh using stored refresh_token or provided one
          5. Raise if none of the above works
        """
        # In-memory check
        if self._token and self._is_fresh(self._token):
            return self._token["access_token"]

        # Env-var based token (Secret Manager fallback — no disk needed)
        raw = os.environ.get("OPEN_API_TOKEN_JSON", "").strip()
        if raw:
            try:
                env_token = json.loads(raw)
                if self._is_fresh(env_token):
                    self._token = env_token
                    return env_token["access_token"]
            except (json.JSONDecodeError, KeyError):
                log.warning("OPEN_API_TOKEN_JSON is invalid or expired")

        # Disk check
        disk_token = self._load_token()
        if disk_token and self._is_fresh(disk_token):
            self._token = disk_token
            return disk_token["access_token"]

        # Try refresh
        rt = refresh_token or (disk_token or {}).get("refresh_token")
        if rt:
            try:
                new_token = self._refresh(rt)
                self._token = new_token
                self._save_token(new_token)
                return new_token["access_token"]
            except Exception as exc:
                log.error("Token refresh failed: %s", exc)

        raise RuntimeError(
            "No valid access token. Run: python -m broker.open_api_auth"
        )

    def exchange_code(self, auth_code: str, redirect_uri: str = REDIRECT_URI) -> dict[str, Any]:
        """Exchange an authorization code for access + refresh tokens."""
        params = urlencode({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        data = self._post(params)
        token = self._parse_response(data)
        self._token = token
        self._save_token(token)
        log.info("Tokens saved — access token valid until %s",
                 time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(token["expires_at"])))
        return token

    # ── Internal ──────────────────────────────────────────────────────────

    def _refresh(self, refresh_token: str) -> dict[str, Any]:
        """Use a refresh token to get a new access token."""
        params = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        data = self._post(params)
        return self._parse_response(data)

    def _post(self, params: str) -> bytes:
        url = f"{TOKEN_HOST}{TOKEN_PATH}?{params}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=30) as resp:
            return resp.read()

    def _parse_response(self, raw: bytes) -> dict[str, Any]:
        import json as _json
        data = _json.loads(raw)
        error_code = data.get("errorCode")
        if error_code:
            desc = data.get("description", "Unknown error")
            raise RuntimeError(f"OAuth error {error_code}: {desc}")
        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")
        expires_in = data.get("expiresIn", 2628000)
        if not access_token or not refresh_token:
            raise RuntimeError(f"Unexpected OAuth response: {data}")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": int(time.time()) + expires_in,
        }

    @staticmethod
    def _is_fresh(token: dict[str, Any], buffer_seconds: int = 86400) -> bool:
        """True if token has at least `buffer_seconds` before expiry."""
        expires_at = token.get("expires_at", 0)
        return int(time.time()) < (expires_at - buffer_seconds)

    def _load_token(self) -> Optional[dict[str, Any]]:
        if not os.path.isfile(TOKEN_FILE):
            return None
        try:
            with open(TOKEN_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_token(self, token: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(token, fh, indent=2)
        log.debug("Token saved to %s", TOKEN_FILE)


# ── CLI entrypoint for initial auth ──────────────────────────────────────────

def _load_dotenv(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_dotenv(os.path.join(base, ".env"))

    client_id = os.environ.get("OPEN_API_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OPEN_API_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Error: OPEN_API_CLIENT_ID and OPEN_API_CLIENT_SECRET must be set in .env")
        return

    auth = OpenApiAuth(client_id, client_secret)

    # Check if we already have a valid token
    try:
        token = auth.get_valid_token()
        print(f"Already have a valid access token (expires "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(token['expires_at']))})")
        return
    except RuntimeError:
        pass

    auth_url = OpenApiAuth.get_auth_url(client_id)
    print("\n" + "=" * 60)
    print("  cTrader Open API — Authorization Required")
    print("=" * 60)
    print("\n1. Visit this URL in your browser:")
    print(f"\n   {auth_url}\n")
    print("2. Log in and click 'Allow access'")
    print("3. You'll be redirected to a blank page (it will fail to load)")
    print("4. Copy the full redirect URL from your browser's address bar")
    print("5. Paste it below and press Enter\n")

    redirect_result = input("Paste the full redirect URL: ").strip()
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_result)
    query = parse_qs(parsed.query)
    auth_code = query.get("code", [None])[0]

    if not auth_code:
        print("Error: No 'code' parameter found in the URL.")
        print(f"Parsed query: {query}")
        return

    try:
        token = auth.exchange_code(auth_code)
        print(f"\nSuccess! Access token valid until "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(token['expires_at']))}")
        print(f"Token stored in: {TOKEN_FILE}")
    except Exception as exc:
        print(f"Error exchanging code: {exc}")


if __name__ == "__main__":
    main()
