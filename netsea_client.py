"""Thin wrapper around the NETSEA Buyer API (https://api.netsea.jp/docs/buyer/).

CEO: 「Amazon以外にネット系の卸売り業者からの仕入れも考えたほうがよいと考えます」
「netseaはすでに登録終わってます」— 御社が発行したAPIトークン(NETSEA_ACCESS_TOKEN)を
使い、取引可能なサプライヤーの商品一覧・在庫・卸価格を取得する。

Only the two endpoints needed for JAN-code-matched sourcing are implemented:
  - GET  /suppliers  -> tradeable supplier list (paginated via next_supplier_id)
  - POST /items      -> product/price/stock data for given supplier_ids
                        (verified empirically: rejects more than ~10-19
                        supplier_ids per call with "too many supplier_ids" -
                        callers must batch in groups of BATCH_SIZE)

NETSEA prices are plain integer yen (no subdivision, unlike Keepa's smallest-
currency-unit convention) - see each item's `set[].price`/`set_price`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "https://api.netsea.jp/buyer/v1"

# 実測: 10件は通るが、それより多い(20件でも)と "too many supplier_ids" で
# 400が返る(api.netsea.jp/docs/buyer/paths/items.jsonには明記されていない
# 実運用上の上限)。呼び出し側でこの単位に分割する。
BATCH_SIZE = 10


class NetseaError(RuntimeError):
    """Raised when the NETSEA API returns an error or an unexpected payload."""


def _request(path: str, access_token: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not access_token:
        raise NetseaError("NETSEA_ACCESS_TOKEN が未設定です。")

    url = f"{BASE_URL}{path}"
    body: Optional[bytes] = None
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if method == "POST":
        clean = {k: v for k, v in (data or {}).items() if v is not None}
        body = urllib.parse.urlencode(clean).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        try:
            message = json.loads(detail).get("error", {}).get("message") or detail
        except json.JSONDecodeError:
            message = detail
        raise NetseaError(f"NETSEA API HTTP {exc.code}: {message[:500]}") from exc
    except urllib.error.URLError as exc:
        raise NetseaError(f"NETSEA API request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NetseaError(f"NETSEA API returned non-JSON response: {raw[:200]!r}") from exc
    # 一部のレスポンス(空の絞り込み結果等)はdictでなく素のlistで返る実装差異が
    # 実測で確認できたため、両方を許容し呼び出し側で正規化する。
    if isinstance(parsed, list):
        return {"data": parsed}
    return parsed


def get_suppliers(access_token: str, next_supplier_id: Optional[str] = None) -> Dict[str, Any]:
    """取引可能な(即時取引対応の)サプライヤー一覧。最大100件/回、
    next_supplier_idでページング。"""
    path = "/suppliers"
    if next_supplier_id:
        path += f"?{urllib.parse.urlencode({'next_supplier_id': next_supplier_id})}"
    return _request(path, access_token, method="GET")


def get_items(
    access_token: str,
    supplier_ids: List[str],
    category_id: Optional[str] = None,
    price_range_from: Optional[int] = None,
    price_range_to: Optional[int] = None,
    sold_out_flag: Optional[str] = "N",
    next_direct_item_id: Optional[str] = None,
) -> Dict[str, Any]:
    """指定サプライヤーの商品一覧(価格・在庫・JANコード等)。

    supplier_idsは最大BATCH_SIZE(10)件まで - それを超える場合は呼び出し側で
    分割すること(find_netsea_items_batched()がそれを行う)。
    """
    if not supplier_ids:
        raise NetseaError("supplier_ids は最低1件必要です。")
    if len(supplier_ids) > BATCH_SIZE:
        raise NetseaError(
            f"supplier_ids が{len(supplier_ids)}件 - 1回の呼び出しでは最大{BATCH_SIZE}件まで"
            "(NETSEA APIの実運用上の制約)。呼び出し側でバッチ分割してください。"
        )
    data = {
        "supplier_ids": ",".join(supplier_ids),
        "category_id": category_id,
        "price_range_from": price_range_from,
        "price_range_to": price_range_to,
        "sold_out_flag": sold_out_flag,
        "next_direct_item_id": next_direct_item_id,
    }
    return _request("/items", access_token, method="POST", data=data)
