"""Thin wrapper around the Keepa API (https://keepa.com/#!api).

Only the endpoints needed by the MCP tools are implemented:
  - GET /search   (type=category)   -> category lookup by name
  - GET /query    (selection=...)   -> Product Finder
  - GET /product  (asin=... | code=...) -> product details / stats / history

Prices in Keepa responses are integers in the *smallest unit* of the local
currency (cents for USD/EUR/GBP, whole yen for JPY - yen has no subdivision).
CSV/stats history is indexed by a fixed "csv type" - see CsvType below.
"""
from __future__ import annotations

import gzip
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "https://api.keepa.com"

# Keepa "domain" ids -> Amazon locale
DOMAIN_IDS: Dict[str, int] = {
    "US": 1,
    "GB": 2,
    "DE": 3,
    "FR": 4,
    "JP": 5,
    "CA": 6,
    "CN": 7,
    "IT": 8,
    "ES": 9,
    "IN": 10,
    "MX": 11,
    "BR": 12,
}

# Smallest currency unit divisor per domain (most are cents; JPY has none).
CURRENCY_DIVISOR: Dict[str, int] = {code: 1 if code == "JP" else 100 for code in DOMAIN_IDS}
CURRENCY_CODE: Dict[str, str] = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "JP": "JPY",
    "CA": "CAD", "CN": "CNY", "IT": "EUR", "ES": "EUR", "IN": "INR",
    "MX": "MXN", "BR": "BRL",
}

# CSV / stats array type indices (see Keepa "Csv Type" reference).
class CsvType:
    AMAZON = 0
    NEW = 1
    USED = 2
    SALES = 3
    LISTPRICE = 4
    RATING = 16
    COUNT_REVIEWS = 17
    BUY_BOX_SHIPPING = 18

NO_DATA_SENTINELS = (-1, -2)

# Keepa's Product Finder rejects perPage < 50 with a 400 "combination of
# perPage and page exeeds limit or is too small" error (verified empirically).
MIN_FINDER_PER_PAGE = 50


class KeepaError(RuntimeError):
    """Raised when the Keepa API returns an error or an unexpected payload."""


def resolve_domain(domain: str) -> int:
    key = (domain or "US").upper()
    if key not in DOMAIN_IDS:
        raise KeepaError(f"Unknown domain '{domain}'. Valid values: {', '.join(DOMAIN_IDS)}")
    return DOMAIN_IDS[key]


def _request(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    clean_params = {k: v for k, v in params.items() if v is not None}
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(clean_params)}"
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        with opener.open(request, timeout=30) as response:
            body = response.read()
            if "gzip" in response.headers.get("Content-Encoding", "").lower():
                body = gzip.decompress(body)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if "gzip" in exc.headers.get("Content-Encoding", "").lower():
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass  # not actually gzip-encoded; fall through and decode as-is
        detail = raw.decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or parsed.get("error") or detail
        except json.JSONDecodeError:
            message = detail
        raise KeepaError(f"Keepa API HTTP {exc.code}: {message[:500]}") from exc
    except urllib.error.URLError as exc:
        raise KeepaError(f"Keepa API request failed: {exc.reason}") from exc

    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError as exc:
        raise KeepaError(f"Keepa API returned non-JSON response: {body[:200]!r}") from exc

    if isinstance(data, dict) and data.get("error"):
        raise KeepaError(f"Keepa API error: {data['error']}")

    return data


def get_token_status(api_key: str) -> Dict[str, Any]:
    """Check the current token bucket balance. Costs 0 tokens (per Keepa docs).

    Returns tokensLeft, refillIn (ms until the bucket is full again), and
    refillRate (tokens generated per minute by the plan).
    """
    data = _request("/token", {"key": api_key})
    return {
        "tokens_left": data.get("tokensLeft"),
        "refill_in_ms": data.get("refillIn"),
        "refill_rate_per_minute": data.get("refillRate"),
    }


# --- Token cost estimators (see https://keepa.com/api-docs/), used for
# preflight balance checks before running multi-call pipelines. These are
# best-effort estimates based on Keepa's published cost formulas, not exact
# guarantees - actual cost can differ slightly (e.g. rounding of "per 100
# ASINs in the result set").

def estimate_finder_cost(per_page: int) -> int:
    """Product Finder (/query): 10 tokens base + 1 token per 100 ASINs
    in the returned page (Keepa enforces a minimum page size of 50)."""
    keepa_per_page = max(MIN_FINDER_PER_PAGE, min(per_page, 200))
    return 10 + math.ceil(keepa_per_page / 100)


def estimate_product_request_cost(count: int) -> int:
    """Product Request (/product): 1 token per ASIN/code requested.

    This assumes get_products()'s params: `buybox` is NOT passed (it would
    make this 5 tokens/product instead of 1 - see get_products()'s
    docstring), and `offers` is never requested. `rating`/`history`/`stats`
    are passed but not believed to add cost beyond the base 1/product
    (unverified beyond the empirical token-balance checks done so far).
    """
    return max(0, count)


def estimate_buybox_product_request_cost(count: int) -> int:
    """Product Request with buybox=1: 5 tokens per product instead of 1 -
    only used narrowly for seller discovery (see get_products(include_buybox=True))."""
    return max(0, count) * 5


def estimate_seller_lookup_cost(count: int) -> int:
    """Seller Information (/seller): 1 token per seller id (storefront=True
    doesn't add cost - Keepa docs say it triggers no new data collection)."""
    return max(0, count)


def estimate_offers_product_request_cost(count: int, offers_limit: int = 20) -> int:
    """Product Request with offers=N: unlike buybox's confirmed 5x, the exact
    formula for `offers` isn't pinned down empirically - public docs only
    say each offer/page adds tokens beyond the base 1/product. This is a
    deliberately generous (over-)estimate (1 page worth, ~6 tokens, per
    product - matching what an earlier pass at these docs found for "per
    offer page") so wait_for_tokens budget checks err toward waiting rather
    than under-budgeting and hitting 429s. Revise once observed live (see
    get_products()'s docstring)."""
    return max(0, count) * 7


def search_categories(api_key: str, term: str, domain: str = "US") -> List[Dict[str, Any]]:
    """Find category ids/names matching a search term (category names are in the
    target marketplace's language, e.g. English for US, Japanese for JP)."""
    data = _request("/search", {
        "key": api_key,
        "domain": resolve_domain(domain),
        "type": "category",
        "term": term,
    })
    categories = data.get("categories") or {}
    results = []
    for cat_id, cat in categories.items():
        results.append({
            "category_id": int(cat_id),
            "name": cat.get("name"),
            "context_free_name": cat.get("contextFreeName"),
            "product_count": cat.get("productCount"),
            "parent_id": cat.get("parent"),
        })
    return results


def get_categories(api_key: str, category_ids: List[int], domain: str = "US") -> Dict[int, Dict[str, Any]]:
    """Category Lookup: full category objects (children, relatedCategories,
    topBrands, etc.) for up to 10 ids per call - all for 1 token regardless
    of how many ids are batched in. Used to expand a seed keyword/category
    into related search terms (see server.py's expand_keyword)."""
    if not category_ids:
        return {}
    ids = category_ids[:10]
    data = _request("/category", {
        "key": api_key,
        "domain": resolve_domain(domain),
        "category": ",".join(str(i) for i in ids),
        "parents": 0,
    })
    categories = data.get("categories") or {}
    return {int(cat_id): cat for cat_id, cat in categories.items() if cat}


def find_products(
    api_key: str,
    domain: str = "US",
    keyword: Optional[str] = None,
    category_id: Optional[int] = None,
    price_min: Optional[int] = None,
    require_amazon_out_of_stock: bool = False,
    monthly_sold_peak_min: Optional[int] = None,
    product_type: Optional[List[str]] = None,
    sales_rank_min: Optional[int] = None,
    sales_rank_max: Optional[int] = None,
    review_count_max: Optional[int] = None,
    review_count_min: Optional[int] = None,
    sort: Optional[List[List[str]]] = None,
    page: int = 0,
    per_page: int = 50,
) -> Dict[str, Any]:
    """Product Finder: cheap, coarse filtering. Does not return price data -
    fetch full product details separately.

    `keyword` is the primary filter (matched against the product title,
    space-separated terms all required, Keepa keyword search - same as the
    dashboard's manual Finder search); `category_id` is an optional
    additional narrowing filter, combined with AND when both are given.

    `price_min` / `require_amazon_out_of_stock` / `monthly_sold_peak_min` /
    `product_type` mirror the dashboard's manual "在庫切れ候補を検索" recipe
    (see keepa-csv-dashboard/sqlite_api_server.py's
    build_keepa_finder_selection): Amazon itself has no offer (only 3rd-party
    sellers do) and the buy-box price clears a floor - i.e. "Amazonに在庫が
    ない、価格が閾値以上" niche/reseller-only listings. `price_min` is in the
    domain's smallest currency unit (cents for USD, whole yen for JPY - so
    price_min=3000 means $30 on domain="US" but Y3000 on domain="JP").

    Keepa requires perPage >= 50 (a 400 error otherwise); the result is
    trimmed back down to the caller's requested `per_page` before returning.
    """
    requested = max(1, per_page)
    keepa_per_page = max(MIN_FINDER_PER_PAGE, min(requested, 200))
    selection: Dict[str, Any] = {
        "page": page,
        "perPage": keepa_per_page,
        "sort": sort if sort is not None else [["current_SALES", "asc"], ["monthlySold", "desc"]],
    }
    if keyword:
        selection["title"] = keyword
    if category_id is not None:
        selection["categories_include"] = [category_id]
    if product_type is not None:
        selection["productType"] = product_type
    if require_amazon_out_of_stock:
        selection["current_AMAZON_gte"] = -1
        selection["current_AMAZON_lte"] = -1
    if price_min is not None:
        selection["current_BUY_BOX_SHIPPING_gte"] = price_min
    if monthly_sold_peak_min is not None:
        selection["monthlySoldPeak_gte"] = monthly_sold_peak_min
    if sales_rank_min is not None:
        selection["current_SALES_gte"] = sales_rank_min
    if sales_rank_max is not None:
        selection["current_SALES_lte"] = sales_rank_max
    if review_count_min is not None:
        selection["current_COUNT_REVIEWS_gte"] = review_count_min
    if review_count_max is not None:
        selection["current_COUNT_REVIEWS_lte"] = review_count_max

    data = _request("/query", {
        "key": api_key,
        "domain": resolve_domain(domain),
        "selection": json.dumps(selection),
    })
    return {
        "asins": (data.get("asinList") or [])[:requested],
        "total_results": data.get("totalResults"),
    }


def get_products(
    api_key: str,
    domain: str = "US",
    asins: Optional[List[str]] = None,
    codes: Optional[List[str]] = None,
    stats_days: int = 90,
    history: bool = False,
    include_buybox: bool = False,
    offers_limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch full product data (price stats, identifiers, rank, reviews).

    Note: `offers` is intentionally omitted - Keepa rejects `offers=0` with a
    400 invalidParameter error (verified empirically); leaving it out avoids
    fetching/paying for live marketplace offers we don't use.

    Note: `buybox` is also omitted by default - it costs 5 tokens per product
    instead of 1 (verified empirically: a 20-ASIN batch drained the token
    bucket by ~100 tokens instead of the expected ~20, confirmed via Keepa API
    discussion/GitHub sources - see estimate_product_request_cost()). It isn't
    needed for ordinary price lookups: analysis.current_price()/
    price_volatility_ratio() already read CsvType.BUY_BOX_SHIPPING (csv/stats
    type 18) straight from the `stats`/`history` data and fall back to
    NEW/AMAZON when it's unavailable - the same approach
    keepa-csv-dashboard/sqlite_api_server.py already uses successfully
    without ever passing buybox=1.

    Pass include_buybox=True only when you specifically need
    buyBoxSellerIdHistory (see analysis.current_buy_box_seller_id) - e.g. to
    find out who's selling an already-qualified candidate, for seller-based
    expansion (see server.py's expand_from_seller). This is the one place in
    the pipeline that intentionally pays the 5x cost, and CEO-approved
    specifically for that narrow use (only on already-qualified candidates,
    not on every candidate evaluated).

    Pass offers_limit=N (e.g. 20) to additionally request the `offers` array
    (every current marketplace listing for the ASIN, not just the buy box
    winner - each has its own `sellerId`, see analysis.distinct_seller_ids).
    This is what powers "Other sellers on Amazon"-style seller discovery
    (server.py's find_other_sellers_for_candidate): one qualified candidate
    can surface several sellers worth mining at once instead of just the buy
    box winner. Cost is not precisely confirmed empirically the way buybox's
    5x was (public docs only say "each offer uses additional tokens" without
    a number) - treat estimate_offers_product_request_cost() as a rough
    upper bound until observed live, and only use this on candidates already
    worth digging into, same as include_buybox.
    """
    if not asins and not codes:
        return []
    params = {
        "key": api_key,
        "domain": resolve_domain(domain),
        "stats": stats_days,
        "history": 1 if history else 0,
        "rating": 1,
    }
    if include_buybox:
        params["buybox"] = 1
    if offers_limit is not None:
        params["offers"] = offers_limit
    if asins:
        params["asin"] = ",".join(asins)
    if codes:
        params["code"] = ",".join(codes)

    data = _request("/product", params)
    products = data.get("products")
    if products is None:
        raise KeepaError("Keepa API response missing 'products' field")
    return products


def lookup_by_code(api_key: str, code: str, domain: str) -> List[Dict[str, Any]]:
    """Cross-domain lookup: find product(s) in `domain` sharing this UPC/EAN/ISBN."""
    return get_products(api_key, domain=domain, codes=[code], stats_days=90, history=False)


def get_sellers(
    api_key: str, seller_ids: List[str], domain: str = "US", storefront: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Seller Information (/seller): 1 token per seller id, batched up to 100
    per call. Passing storefront=True additionally returns each seller's
    listed ASINs (field `asinList`, up to 100,000) at no extra token cost
    (Keepa docs: the storefront flag doesn't trigger new data collection,
    just includes what Keepa already has on file) - this is what powers
    "seller mining": find who's selling an already-qualified candidate, then
    pull the rest of their catalog as new candidates (see server.py's
    expand_from_seller).
    """
    if not seller_ids:
        return {}
    ids = seller_ids[:100]
    params = {
        "key": api_key,
        "domain": resolve_domain(domain),
        "seller": ",".join(ids),
    }
    if storefront:
        params["storefront"] = 1
    data = _request("/seller", params)
    sellers = data.get("sellers") or {}
    return {seller_id: info for seller_id, info in sellers.items() if info}
