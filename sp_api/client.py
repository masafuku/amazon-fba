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
    body: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Call one SP-API REST endpoint, retrying once on 429/5xx per Retry-After."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    query = f"?{urllib.parse.urlencode(clean_params)}" if clean_params else ""
    url = f"{endpoint()}{path}{query}"
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else None

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        access_token = get_access_token()
        headers = {
            "x-amz-access-token": access_token,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        if body_bytes is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
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


def get_order_items(order_id: str, next_token: Optional[str] = None) -> Dict[str, Any]:
    """Orders API: getOrderItems - 注文内の商品明細(ASIN/SKU/数量/単価)。
    getOrdersの返り値自体にはASINが含まれないため、商品ごとのP&Lにはこちらが必要。"""
    params = {"NextToken": next_token} if next_token else None
    return _request(f"/orders/v0/orders/{order_id}/orderItems", params)


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


def get_inbound_plans(next_token: Optional[str] = None) -> Dict[str, Any]:
    """Fulfillment Inbound API(v2024-03-20): listInboundPlans - Send to Amazonで
    作成した納品プラン一覧(FBA納品便ごとのP&L用)。"""
    params = {"pageSize": 20, "NextToken": next_token} if next_token else {"pageSize": 20}
    return _request("/inbound/fba/2024-03-20/inboundPlans", params)


def get_inbound_plan(plan_id: str) -> Dict[str, Any]:
    """getInboundPlan - プラン詳細(埋め込みのshipments配列にshipmentId/statusが入る)。"""
    return _request(f"/inbound/fba/2024-03-20/inboundPlans/{plan_id}")


def get_inbound_plan_items(plan_id: str, next_token: Optional[str] = None) -> Dict[str, Any]:
    """listInboundPlanItems - プラン内の商品明細(ASIN/SKU/数量)。"""
    params = {"NextToken": next_token} if next_token else None
    return _request(f"/inbound/fba/2024-03-20/inboundPlans/{plan_id}/items", params)


def get_inbound_shipment(plan_id: str, shipment_id: str) -> Dict[str, Any]:
    """getShipment - 便ごとの詳細(納品先FC、納品期間、実際のFBA Shipment ID等)。"""
    return _request(f"/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}")


def put_listing_item(
    sku: str,
    asin: str,
    product_type: str,
    price_usd: float,
    quantity: int = 0,
    condition: str = "new_new",
    seller_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Listings Items API: putListingsItem - 既存ASINへの出品オファーを作成/更新する。

    FBA出品のため quantity は 0 (在庫はAmazon倉庫管理)。
    price_usd はドル建て価格。自動価格設定は Seller Central から別途設定。
    """
    seller = seller_id or settings.seller_id
    if not seller:
        raise SpApiError("販売者ID(SP_API_SELLER_ID)が未設定です。")

    # Encode SKU for URL path
    encoded_sku = urllib.parse.quote(sku, safe="")

    body = {
        "productType": product_type,
        "requirements": "LISTING_OFFER_ONLY",
        "attributes": {
            "merchant_suggested_asin": [{"value": asin, "marketplace_id": settings.marketplace_id}],
            "condition_type": [{"value": condition, "marketplace_id": settings.marketplace_id}],
            "purchasable_offer": [{
                "currency": "USD",
                "our_price": [{"schedule": [{"value_with_tax": price_usd}]}],
                "marketplace_id": settings.marketplace_id,
            }],
            "fulfillment_availability": [{
                "fulfillment_channel_code": "AMAZON_NA",
                "quantity": quantity,
                "marketplace_id": settings.marketplace_id,
            }],
            "batteries_required": [{"value": False, "marketplace_id": settings.marketplace_id}],
            "supplier_declared_dg_hz_regulation": [{"value": "not_applicable", "marketplace_id": settings.marketplace_id}],
        },
    }

    return _request(
        f"/listings/2021-08-01/items/{seller}/{encoded_sku}",
        {"marketplaceIds": settings.marketplace_id},
        method="PUT",
        body=body,
    )


def search_listings_items(next_token: Optional[str] = None, page_size: int = 20) -> Dict[str, Any]:
    """Listings Items API: searchListingsItems - 出品中SKU一覧(価格・状態付き)。"""
    seller = settings.seller_id
    if not seller:
        raise SpApiError("販売者ID(SP_API_SELLER_ID)が未設定です。")
    params = {
        "marketplaceIds": settings.marketplace_id,
        "includedData": "summaries,offers",
        "pageSize": page_size,
        "pageToken": next_token,
    }
    return _request(f"/listings/2021-08-01/items/{seller}", params)


def get_fees_estimate(asin: str, price_usd: float) -> Dict[str, float]:
    """Product Fees API: getMyFeesEstimateForASIN - 指定価格でFBA出品したときの手数料見積もり(USD)。
    返り値: {referral, fba, total}。他の費目(クロージング料等)は other に入る。"""
    body = {"FeesEstimateRequest": {
        "MarketplaceId": settings.marketplace_id,
        "IsAmazonFulfilled": True,
        "Identifier": f"{asin}-{price_usd}",
        "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": price_usd}},
    }}
    for attempt in range(4):
        result = _request(f"/products/fees/v0/items/{asin}/feesEstimate", method="POST", body=body)
        estimate = result["payload"]["FeesEstimateResult"]
        if estimate.get("Status") == "Success":
            break
        # 200で返ってくる内部エラー(InternalError)は一時的なので待って再試行する
        if (estimate.get("Error") or {}).get("Code") == "InternalError" and attempt < 3:
            time.sleep(3 * (attempt + 1))
            continue
        raise SpApiError(f"手数料見積もり失敗 {asin}: {estimate.get('Error')}")
    amounts = {d["FeeType"]: float(d["FeeAmount"]["Amount"]) for d in estimate["FeesEstimate"]["FeeDetailList"]}
    total = float(estimate["FeesEstimate"]["TotalFeesEstimate"]["Amount"])
    referral, fba = amounts.get("ReferralFee", 0.0), amounts.get("FBAFees", 0.0)
    return {"referral": referral, "fba": fba, "total": total, "other": round(total - referral - fba, 4)}


def get_offers_summary(asin: str) -> Dict[str, Any]:
    """Product Pricing API: getItemOffers - 新品のバイボックス価格・FBA最安値・出品数(USD)。
    バイボックス/最安値は出品者がいなければNone。上限は毎秒0.5回なので、連続で呼ぶ側が2秒以上空ける。"""
    result = _request(
        f"/products/pricing/v0/items/{asin}/offers",
        {"MarketplaceId": settings.marketplace_id, "ItemCondition": "New", "CustomerType": "Consumer"},
    )
    summary = result.get("payload", {}).get("Summary") or {}
    prices = summary.get("BuyBoxPrices") or []
    fba = [float(p["ListingPrice"]["Amount"]) for p in summary.get("LowestPrices", []) if p.get("fulfillmentChannel") == "Amazon"]
    return {
        "buy_box": float(prices[0]["ListingPrice"]["Amount"]) if prices else None,
        "lowest_fba": min(fba) if fba else None,
        "offers": sum(int(n.get("OfferCount", 0)) for n in summary.get("NumberOfOffers", [])),
    }


def get_buy_box_price(asin: str) -> Optional[float]:
    """新品のバイボックス価格(USD)。出品者がいなければNone。"""
    return get_offers_summary(asin)["buy_box"]


def get_pricing_rule_ids(product_type: str) -> list:
    """この出品者が使える自動価格設定ルールのID一覧(商品タイプ定義の seller 固有 enum)。"""
    definition = _request(
        f"/definitions/2020-09-01/productTypes/{product_type}",
        {"marketplaceIds": settings.marketplace_id, "requirements": "LISTING_OFFER_ONLY",
         "locale": "en_US", "sellerId": settings.seller_id},
    )
    schema = json.load(urllib.request.urlopen(definition["schema"]["link"]["resource"], timeout=30))
    rule = (schema["properties"]["purchasable_offer"]["items"]["properties"]
            ["automated_pricing_merchandising_rule_plan"]["items"]["properties"]["merchandising_rule"]["properties"]["rule_id"])
    return rule.get("enum") or []


def enroll_pricing_rule(
    sku: str,
    product_type: str,
    rule_id: str,
    current_price_usd: float,
    min_price_usd: float,
    max_price_usd: float,
    validate_only: bool = True,
) -> Dict[str, Any]:
    """Listings Items API: patchListingsItem - SKUを自動価格設定ルールに紐付ける。

    purchasable_offer は replace で丸ごと置き換わるため、現在価格(our_price)も一緒に渡す
    (渡さないと価格が消える)。validate_only=True は VALIDATION_PREVIEW(検証のみで反映しない)。
    ルールの反映は非同期。確認は get_listing_item 相当(searchListingsItems)で行う。
    """
    seller = settings.seller_id
    if not seller:
        raise SpApiError("販売者ID(SP_API_SELLER_ID)が未設定です。")
    marketplace = settings.marketplace_id
    body = {
        "productType": product_type,
        "patches": [{
            "op": "replace",
            "path": "/attributes/purchasable_offer",
            "value": [{
                "currency": "USD",
                "audience": "ALL",
                "marketplace_id": marketplace,
                "our_price": [{"schedule": [{"value_with_tax": current_price_usd}]}],
                "minimum_seller_allowed_price": [{"schedule": [{"value_with_tax": min_price_usd}]}],
                "maximum_seller_allowed_price": [{"schedule": [{"value_with_tax": max_price_usd}]}],
                "automated_pricing_merchandising_rule_plan": [{"merchandising_rule": {"rule_id": rule_id}}],
            }],
        }],
    }
    params = {"marketplaceIds": marketplace, "mode": "VALIDATION_PREVIEW" if validate_only else None}
    return _request(
        f"/listings/2021-08-01/items/{seller}/{urllib.parse.quote(sku, safe='')}",
        params,
        method="PATCH",
        body=body,
    )


def get_listings_restrictions(
    asin: str,
    condition_type: str = "new_new",
    seller_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Listings Restrictions API: getListingsRestrictions - このアカウントが、そのASINを
    指定の状態(既定: 新品)で出品できるかを照会する。出品は行わない(照会のみ)。

    返り値の "restrictions" が空なら出品可。要承認なら reasonCode="APPROVAL_REQUIRED"
    (links に承認申請のURL)、出品不可なら reasonCode="NOT_ELIGIBLE" などが入る。
    販売者ID(SP_API_SELLER_ID)が必要。
    """
    seller = seller_id or settings.seller_id
    if not seller:
        raise SpApiError("販売者ID(SP_API_SELLER_ID)が未設定です。Seller Central の「設定 > アカウント情報」で確認して .env に設定してください。")
    return _request(
        "/listings/2021-08-01/restrictions",
        {
            "asin": asin,
            "sellerId": seller,
            "marketplaceIds": settings.marketplace_id,
            "conditionType": condition_type,
            "reasonLocale": "ja_JP",
        },
    )
