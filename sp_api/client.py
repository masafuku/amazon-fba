"""Thin wrapper around Amazon SP-API (Orders / Finances / FBA Inventory).

Mirrors the shape of keepa_mcp/keepa_client.py: stdlib urllib.request only
(no `requests` dependency, consistent with the rest of this repo), a single
`SpApiError` exception, and small endpoint-specific functions that return
parsed JSON. Unlike Keepa (a single `?key=` query param), SP-API needs an
LWA (Login with Amazon) access token refreshed via OAuth - that token is
fetched lazily and cached in-process until it's close to expiry.

No AWS SigV4 signing / boto3: as of SP-API's 2023 auth simplification, a
self-authorized Private App can call these endpoints with just the LWA
access token in the `x-amz-access-token` header.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from sp_api.config import settings, endpoint

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# Refresh the access token a bit before Amazon's stated 1-hour expiry so a
# long-running sync cycle never gets caught with a stale token mid-call.
_TOKEN_REFRESH_MARGIN_SECONDS = 60

_access_token: Optional[str] = None
_access_token_expires_at: float = 0.0


class SpApiError(RuntimeError):
    """Raised when SP-API returns an error or an unexpected payload."""


def _fetch_access_token() -> str:
    if not settings.configured:
        raise SpApiError(
            "SP-API credentials are not configured (LWA_CLIENT_ID / "
            "LWA_CLIENT_SECRET / SP_API_REFRESH_TOKEN missing from .env)."
        )
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": settings.refresh_token,
            "client_id": settings.lwa_client_id,
            "client_secret": settings.lwa_client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        LWA_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SpApiError(f"LWA token refresh HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SpApiError(f"LWA token refresh failed: {exc.reason}") from exc

    token = payload.get("access_token")
    expires_in = payload.get("expires_in", 3600)
    if not token:
        raise SpApiError(f"LWA token refresh returned no access_token: {payload}")
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


def _request(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    method: str = "GET",
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Call one SP-API REST endpoint, retrying once on 429/5xx per Retry-After."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    query = f"?{urllib.parse.urlencode(clean_params)}" if clean_params else ""
    url = f"{endpoint()}{path}{query}"

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        access_token = get_access_token()
        request = urllib.request.Request(
            url,
            headers={
                "x-amz-access-token": access_token,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                if "gzip" in response.headers.get("Content-Encoding", "").lower():
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                # Access token may have just expired early; force one refresh.
                get_access_token(force_refresh=True)
                last_error = exc
                continue
            if exc.code == 429 or exc.code >= 500:
                retry_after = float(exc.headers.get("Retry-After", 2 ** attempt))
                last_error = exc
                time.sleep(min(retry_after, 30))
                continue
            detail = exc.read().decode("utf-8", errors="ignore")
            raise SpApiError(f"SP-API HTTP {exc.code} on {path}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise SpApiError(f"SP-API request to {path} failed: {exc.reason}") from exc

    raise SpApiError(f"SP-API request to {path} failed after {max_retries} retries: {last_error}")


def get_orders(
    last_updated_after_iso: str,
    next_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Orders API: getOrders. Returns raw payload (caller paginates via NextToken)."""
    if next_token:
        params = {"NextToken": next_token}
    else:
        params = {
            "MarketplaceIds": settings.marketplace_id,
            "LastUpdatedAfter": last_updated_after_iso,
        }
    return _request("/orders/v0/orders", params)


def list_financial_events_by_order(order_id: str) -> Dict[str, Any]:
    """Finances API: listFinancialEventsByOrderId - actual referral/FBA/storage fees."""
    return _request(f"/finances/v0/orders/{order_id}/financialEvents")


def get_inventory_summaries(next_token: Optional[str] = None) -> Dict[str, Any]:
    """FBA Inventory API: getInventorySummaries - current fulfillable quantity per ASIN."""
    if next_token:
        params = {"NextToken": next_token, "granularityType": "Marketplace", "granularityId": settings.marketplace_id, "marketplaceIds": settings.marketplace_id}
    else:
        params = {
            "granularityType": "Marketplace",
            "granularityId": settings.marketplace_id,
            "marketplaceIds": settings.marketplace_id,
            "details": "true",
        }
    return _request("/fba/inventory/v1/summaries", params)
