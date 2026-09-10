"""Thin wrapper around the Gmail API (read-only), used only by
sd_email_parser.py to read Super Delivery order-confirmation emails.

Auth: Google OAuth2 refresh-token flow (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET /
GMAIL_REFRESH_TOKEN in .env), same shape as sp_api/client.py's LWA flow -
fetch + cache an access token, refresh when it's close to expiry. Read-only
scope only (https://www.googleapis.com/auth/gmail.readonly); this module
never sends, modifies, or deletes mail.

stdlib urllib.request only, consistent with the rest of this repo.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENV_FILE = Path(__file__).resolve().parent / ".env"


def _load_dotenv(path: Path = _ENV_FILE) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_REFRESH_MARGIN_SECONDS = 60

_access_token: Optional[str] = None
_access_token_expires_at: float = 0.0


class GmailError(RuntimeError):
    """Raised when the Gmail API returns an error or an unexpected payload."""


def _env(key: str) -> str:
    return os.getenv(key, "")


def configured() -> bool:
    return bool(_env("GMAIL_CLIENT_ID") and _env("GMAIL_CLIENT_SECRET") and _env("GMAIL_REFRESH_TOKEN"))


def _fetch_access_token() -> "tuple[str, float]":
    if not configured():
        raise GmailError(
            "Gmail API credentials are not configured (GMAIL_CLIENT_ID / "
            "GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN missing from .env)."
        )
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": _env("GMAIL_REFRESH_TOKEN"),
            "client_id": _env("GMAIL_CLIENT_ID"),
            "client_secret": _env("GMAIL_CLIENT_SECRET"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise GmailError(f"Gmail token refresh HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GmailError(f"Gmail token refresh failed: {exc.reason}") from exc

    token = payload.get("access_token")
    expires_in = payload.get("expires_in", 3600)
    if not token:
        raise GmailError(f"Gmail token refresh returned no access_token: {payload}")
    return token, float(expires_in)


def get_access_token(force_refresh: bool = False) -> str:
    global _access_token, _access_token_expires_at
    now = time.time()
    if not force_refresh and _access_token and now < _access_token_expires_at:
        return _access_token
    token, expires_in = _fetch_access_token()
    _access_token = token
    _access_token_expires_at = now + expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
    return _access_token


def _request(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    query = f"?{urllib.parse.urlencode(clean_params)}" if clean_params else ""
    url = f"{GMAIL_API_BASE}{path}{query}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise GmailError(f"Gmail API HTTP {exc.code} on {path}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GmailError(f"Gmail API request to {path} failed: {exc.reason}") from exc


def search_messages(query: str, max_results: int = 50) -> List[str]:
    """Returns message ids matching a Gmail search query (e.g. subject:...)."""
    data = _request("/messages", {"q": query, "maxResults": max_results})
    return [m["id"] for m in data.get("messages", [])]


def get_message_plaintext(message_id: str) -> str:
    """Fetches one message and returns its plain-text body (best effort)."""
    data = _request(f"/messages/{message_id}", {"format": "full"})
    return _extract_plaintext(data.get("payload", {}))


def _extract_plaintext(payload: Dict[str, Any]) -> str:
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if mime_type == "text/plain" and body_data:
        return _decode_body(body_data)
    for part in payload.get("parts", []) or []:
        text = _extract_plaintext(part)
        if text:
            return text
    # Fall back to any body we can find, even if not text/plain.
    if body_data:
        return _decode_body(body_data)
    return ""


def _decode_body(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
