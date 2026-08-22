"""Cache-aware wrappers around keepa_client's raw API calls.

Each function checks the local SQLite cache (see cache.py) first and only
calls the live Keepa API on a miss/stale entry or when force_refresh=True.
Live results are written back to the cache. Every function returns a
`cache_info` alongside the data so callers (server.py tools) can tell the
user whether they are looking at cached or fresh data, and how old it is -
important here since prices/ranks genuinely change over time.

If settings.cache_enabled is False, these behave like force_refresh=True
everywhere and never touch the cache (a full opt-out).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import cache
from .config import settings
from .keepa_client import find_products, get_categories, get_products, get_sellers, lookup_by_code, search_categories

CacheInfo = Dict[str, Any]


def _ttl(hours: float) -> float:
    return hours * 3600


def _hit_info(age_seconds: float) -> CacheInfo:
    return {"hit": True, "age_seconds": round(age_seconds, 1)}


def _miss_info() -> CacheInfo:
    return {"hit": False, "age_seconds": 0.0}


def cached_search_categories(
    api_key: str, term: str, domain: str = "US", force_refresh: bool = False
) -> Tuple[List[Dict[str, Any]], CacheInfo]:
    key = f"{domain.upper()}:{term.strip().lower()}"
    if settings.cache_enabled and not force_refresh:
        hit = cache.get("category_search", key, _ttl(settings.cache_ttl_category_hours))
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    result = search_categories(api_key, term, domain=domain)
    if settings.cache_enabled:
        cache.set("category_search", key, result)
    return result, _miss_info()


def cached_get_categories(
    api_key: str, category_ids: List[int], domain: str = "US", force_refresh: bool = False
) -> Tuple[Dict[int, Dict[str, Any]], CacheInfo]:
    """Per-category cache, mirrors cached_get_products: only the ids not
    already cached get fetched live (still batched into one call)."""
    ttl = _ttl(settings.cache_ttl_category_hours)
    result: Dict[int, Dict[str, Any]] = {}
    to_fetch: List[int] = []
    any_hit = False

    for cat_id in category_ids:
        key = f"{domain.upper()}:{cat_id}"
        if settings.cache_enabled and not force_refresh:
            hit = cache.get("category_lookup", key, ttl)
            if hit is not None:
                value, _age = hit
                result[cat_id] = value
                any_hit = True
                continue
        to_fetch.append(cat_id)

    if to_fetch:
        fetched = get_categories(api_key, to_fetch, domain=domain)
        for cat_id, cat in fetched.items():
            result[cat_id] = cat
            if settings.cache_enabled:
                cache.set("category_lookup", f"{domain.upper()}:{cat_id}", cat)

    cache_info = _hit_info(0) if (any_hit and not to_fetch) else _miss_info()
    return result, cache_info


def cached_get_sellers(
    api_key: str, seller_ids: List[str], domain: str = "US", storefront: bool = True, force_refresh: bool = False
) -> Tuple[Dict[str, Dict[str, Any]], CacheInfo]:
    """Per-seller-id cache, mirrors cached_get_categories. storefront=True
    (default) includes each seller's listed ASINs (see keepa_client.get_sellers)."""
    ttl = _ttl(settings.cache_ttl_seller_hours)
    result: Dict[str, Dict[str, Any]] = {}
    to_fetch: List[str] = []
    any_hit = False

    for seller_id in seller_ids:
        key = f"{domain.upper()}:{seller_id}:storefront={int(storefront)}"
        if settings.cache_enabled and not force_refresh:
            hit = cache.get("seller", key, ttl)
            if hit is not None:
                value, _age = hit
                result[seller_id] = value
                any_hit = True
                continue
        to_fetch.append(seller_id)

    if to_fetch:
        fetched = get_sellers(api_key, to_fetch, domain=domain, storefront=storefront)
        for seller_id, info in fetched.items():
            result[seller_id] = info
            if settings.cache_enabled:
                cache.set("seller", f"{domain.upper()}:{seller_id}:storefront={int(storefront)}", info)

    cache_info = _hit_info(0) if (any_hit and not to_fetch) else _miss_info()
    return result, cache_info


def cached_find_products(
    api_key: str,
    domain: str,
    keyword: Optional[str],
    category_id: Optional[int],
    sales_rank_min: Optional[int],
    sales_rank_max: Optional[int],
    review_count_max: Optional[int],
    review_count_min: Optional[int],
    per_page: int,
    price_min: Optional[int] = None,
    require_amazon_out_of_stock: bool = False,
    monthly_sold_peak_min: Optional[int] = None,
    product_type: Optional[List[str]] = None,
    force_refresh: bool = False,
) -> Tuple[Dict[str, Any], CacheInfo]:
    key = json.dumps({
        "domain": domain.upper(), "keyword": keyword, "category_id": category_id,
        "sales_rank_min": sales_rank_min, "sales_rank_max": sales_rank_max,
        "review_count_max": review_count_max, "review_count_min": review_count_min,
        "per_page": per_page, "price_min": price_min,
        "require_amazon_out_of_stock": require_amazon_out_of_stock,
        "monthly_sold_peak_min": monthly_sold_peak_min, "product_type": product_type,
    }, sort_keys=True)

    if settings.cache_enabled and not force_refresh:
        hit = cache.get("finder", key, _ttl(settings.cache_ttl_finder_hours))
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    result = find_products(
        api_key, domain=domain, keyword=keyword, category_id=category_id,
        sales_rank_min=sales_rank_min, sales_rank_max=sales_rank_max,
        review_count_max=review_count_max, review_count_min=review_count_min,
        per_page=per_page, price_min=price_min,
        require_amazon_out_of_stock=require_amazon_out_of_stock,
        monthly_sold_peak_min=monthly_sold_peak_min, product_type=product_type,
    )
    if settings.cache_enabled:
        cache.set("finder", key, result)
    return result, _miss_info()


def cached_get_products(
    api_key: str,
    domain: str,
    asins: List[str],
    stats_days: int = 90,
    force_refresh: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, CacheInfo]]:
    """Per-ASIN cache: only the ASINs that are missing/stale are actually
    fetched from Keepa (in a single batched request), so re-running a search
    that overlaps a previous one only pays for the new ASINs.
    """
    ttl = _ttl(settings.cache_ttl_product_hours)
    products: Dict[str, Dict[str, Any]] = {}
    cache_meta: Dict[str, CacheInfo] = {}
    to_fetch: List[str] = []

    for asin in asins:
        key = f"{domain.upper()}:{asin}"
        if settings.cache_enabled and not force_refresh:
            hit = cache.get("product", key, ttl)
            if hit is not None:
                value, age = hit
                products[asin] = value
                cache_meta[asin] = _hit_info(age)
                continue
        to_fetch.append(asin)

    if to_fetch:
        fetched = get_products(api_key, domain=domain, asins=to_fetch, stats_days=stats_days)
        for product in fetched:
            asin = product.get("asin")
            if not asin:
                continue
            products[asin] = product
            cache_meta[asin] = _miss_info()
            if settings.cache_enabled:
                cache.set("product", f"{domain.upper()}:{asin}", product)

    # Preserve the caller's requested order; drop ASINs Keepa didn't return.
    ordered = [products[asin] for asin in asins if asin in products]
    return ordered, cache_meta


def cached_get_product_with_buybox(
    api_key: str, asin: str, domain: str, force_refresh: bool = False
) -> Tuple[Optional[Dict[str, Any]], CacheInfo]:
    """Single-ASIN fetch with buybox=1 (5 tokens - see keepa_client.get_products'
    docstring), cached separately from the plain product cache (kind=
    'product_buybox') since it's a different, more expensive fetch we don't
    want to accidentally treat as interchangeable with the cheap one. Used
    only for seller discovery (server.py's find_seller_for_candidate)."""
    key = f"{domain.upper()}:{asin}"
    ttl = _ttl(settings.cache_ttl_product_hours)
    if settings.cache_enabled and not force_refresh:
        hit = cache.get("product_buybox", key, ttl)
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    products = get_products(api_key, domain=domain, asins=[asin], stats_days=90, include_buybox=True)
    if not products:
        return None, _miss_info()
    product = products[0]
    if settings.cache_enabled:
        cache.set("product_buybox", key, product)
    return product, _miss_info()


def cached_get_product_with_offers(
    api_key: str, asin: str, domain: str, offers_limit: int = 20,
    include_stock: bool = False, force_refresh: bool = False,
) -> Tuple[Optional[Dict[str, Any]], CacheInfo]:
    """Single-ASIN fetch with offers=offers_limit (see keepa_client.get_products'
    docstring - cost not empirically pinned down, treated as pricier than the
    plain product fetch). Cached separately (kind='product_offers') from both
    the plain and buybox product caches. Used for seller discovery
    (server.py's find_other_sellers_for_candidate) and, with
    include_stock=True, for competitor stock totals
    (server.py's enrich_qualified_candidates_with_offer_details).

    include_stock is part of the cache key - a response cached without
    stock=1 has no stockCSV field on its offers, so serving it for an
    include_stock=True request would silently return no stock data."""
    key = f"{domain.upper()}:{asin}:offers={offers_limit}:stock={int(include_stock)}"
    ttl = _ttl(settings.cache_ttl_product_hours)
    if settings.cache_enabled and not force_refresh:
        hit = cache.get("product_offers", key, ttl)
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    products = get_products(
        api_key, domain=domain, asins=[asin], stats_days=90,
        offers_limit=offers_limit, include_stock=include_stock,
    )
    if not products:
        return None, _miss_info()
    product = products[0]
    if settings.cache_enabled:
        cache.set("product_offers", key, product)
    return product, _miss_info()


def cached_get_product_with_history(
    api_key: str, asin: str, domain: str, force_refresh: bool = False
) -> Tuple[Optional[Dict[str, Any]], CacheInfo]:
    """Single-ASIN fetch with history=1 (full CSV time-series, not just the
    current/min/max `stats` summary the rest of the pipeline uses). Cached
    separately (kind='product_history') from the plain product cache - the
    plain cache's key would otherwise collide and could return a cached
    product with no `csv` field for a caller that actually needs history.
    Used only by server.py's get_product_history() (CandidateDetailPage's
    on-demand chart fetch - CEO explicitly wants this button-triggered, not
    automatic, so this is never called from the main scan pipeline)."""
    key = f"{domain.upper()}:{asin}"
    ttl = _ttl(settings.cache_ttl_product_hours)
    if settings.cache_enabled and not force_refresh:
        hit = cache.get("product_history", key, ttl)
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    products = get_products(api_key, domain=domain, asins=[asin], stats_days=90, history=True)
    if not products:
        return None, _miss_info()
    product = products[0]
    if settings.cache_enabled:
        cache.set("product_history", key, product)
    return product, _miss_info()


def is_product_cached(asin: str, domain: str) -> bool:
    """Peek whether a single-ASIN product lookup is currently a fresh cache
    hit, without triggering a live call. Mirrors is_code_lookup_cached()
    but for the ASIN-based cross-domain match (see cached_get_products)."""
    if not settings.cache_enabled:
        return False
    key = f"{domain.upper()}:{asin}"
    return cache.get("product", key, _ttl(settings.cache_ttl_product_hours)) is not None


def is_code_lookup_cached(code: str, domain: str) -> bool:
    """Peek whether a JP/etc. code lookup is currently a fresh cache hit,
    without triggering a live call. Used to let free cache hits through a
    token-budget gate even when the live-call budget is exhausted."""
    if not settings.cache_enabled:
        return False
    key = f"{domain.upper()}:{code}"
    return cache.get("code_lookup", key, _ttl(settings.cache_ttl_product_hours)) is not None


def cached_lookup_by_code(
    api_key: str, code: str, domain: str, force_refresh: bool = False
) -> Tuple[List[Dict[str, Any]], CacheInfo]:
    key = f"{domain.upper()}:{code}"
    ttl = _ttl(settings.cache_ttl_product_hours)
    if settings.cache_enabled and not force_refresh:
        hit = cache.get("code_lookup", key, ttl)
        if hit is not None:
            value, age = hit
            return value, _hit_info(age)

    result = lookup_by_code(api_key, code, domain)
    if settings.cache_enabled:
        cache.set("code_lookup", key, result)
        # Also seed the per-ASIN product cache for any matched products, so
        # a later get_product_detail(asin) call on the same product is free.
        for product in result:
            asin = product.get("asin")
            if asin:
                cache.set("product", f"{domain.upper()}:{asin}", product)
    return result, _miss_info()
