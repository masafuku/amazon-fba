"""MCP server exposing Keepa-backed tools for finding Japan -> North America
Amazon arbitrage candidates.

Run directly for local testing:
    python -m keepa_mcp.server

Registered as a stdio MCP server in ../.mcp.json under the name "keepa".

Caching: results are cached locally in keepa_mcp/cache.sqlite3 (see cache.py /
cached_ops.py) to conserve Keepa tokens, since the same ASIN/category/code is
often re-queried while tuning filters. Every tool that can be served from
cache accepts force_refresh=True to bypass it and pull live data - use that
when you have spare token budget and want current prices/ranks rather than
what was cached earlier.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from . import analysis, cache
from .cached_ops import (
    cached_find_products,
    cached_get_products,
    cached_lookup_by_code,
    cached_search_categories,
    is_product_cached,
)
from .config import settings
from .keepa_client import (
    KeepaError,
    estimate_finder_cost,
    estimate_product_request_cost,
    get_token_status,
)

mcp = FastMCP("keepa-arbitrage-finder")


def _require_api_key() -> str:
    if not settings.keepa_api_key:
        raise KeepaError(
            "KEEPA_API_KEY is not set. Add it to the repository's .env file "
            "(see .env.example) and restart the MCP server."
        )
    return settings.keepa_api_key


def _wait_estimate(shortfall: float, refill_rate_per_minute: Optional[int]) -> str:
    if not refill_rate_per_minute or refill_rate_per_minute <= 0:
        return "unknown (check your plan's refill rate on the Keepa dashboard)"
    minutes = max(1, math.ceil(shortfall / refill_rate_per_minute))
    return f"~{minutes} min at {refill_rate_per_minute} token/min"


# Keep any single wait-mode batch small enough to fit comfortably under a
# low-refill-rate plan's bucket cap (capacity = refill_rate_per_minute x 60,
# e.g. 60 tokens on a 1 token/min plan) - see cached_get_products calls in
# find_arbitrage_candidates(wait_for_tokens=True).
_WAIT_MODE_BATCH_SIZE = 20


def _wait_for_budget(api_key: str, needed: int, current_budget: int) -> int:
    """Block until at least `needed` live-call tokens are available, polling
    the real balance (check_token_balance costs nothing) and sleeping
    between checks. Returns the fresh real balance once satisfied. Only
    call this when the caller has opted into wait_for_tokens=True - it can
    block for a long time on a slow-refill plan."""
    budget = current_budget
    if budget >= needed:
        return budget
    while True:
        status = get_token_status(api_key)
        budget = status["tokens_left"] or 0
        if budget >= needed:
            return budget
        refill_rate = status["refill_rate_per_minute"] or 1
        shortfall = needed - budget
        wait_seconds = min(60, max(5, math.ceil(shortfall * 60 / refill_rate)))
        time.sleep(wait_seconds)


@mcp.tool()
def check_token_balance() -> Dict[str, Any]:
    """Check the current Keepa API token balance. This call itself is free
    (0 tokens) - use it before running find_candidates / find_arbitrage_candidates
    to see whether you have enough budget, especially on low refill-rate plans.
    """
    api_key = _require_api_key()
    return get_token_status(api_key)


@mcp.tool()
def cache_status() -> Dict[str, Any]:
    """Show how many entries are cached locally, by kind (category_search,
    finder, product, code_lookup), and their age range. Free - no Keepa call."""
    return cache.stats()


def _summarize_product(product: Dict[str, Any], domain: str, cache_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    asin = product.get("asin")
    summary = {
        "asin": asin,
        "title": product.get("title"),
        "brand": product.get("brand"),
        "domain": domain.upper(),
        "price": analysis.current_price(product, domain),
        "currency": _currency_for(domain),
        "sales_rank": analysis.sales_rank(product),
        "review_count": analysis.review_count(product),
        "rating": analysis.rating(product),
        "price_volatility_90d": analysis.price_volatility_ratio(product, domain),
        "weight_kg": analysis.package_weight_kg(product),
        "referral_fee_percent": analysis.referral_fee_percent(product),
        "fba_pickpack_fee": analysis.fba_pickpack_fee(product, domain),
        "upc": (product.get("upcList") or [None])[0],
        "ean": (product.get("eanList") or [None])[0],
        "url": analysis.product_url(asin, domain),
    }
    if cache_info is not None:
        summary["_cache"] = cache_info
    return summary


def _currency_for(domain: str) -> str:
    from .keepa_client import CURRENCY_CODE
    return CURRENCY_CODE.get(domain.upper(), "?")


@mcp.tool()
def search_category(term: str, domain: str = "US", force_refresh: bool = False) -> Dict[str, Any]:
    """Search Keepa's category tree by name and return matching category ids.

    Use this first to resolve a category name (e.g. "Kitchen Utensils & Gadgets")
    to the numeric category_id needed by find_candidates / find_arbitrage_candidates.
    Category names are in the target marketplace's language (English for domain=US).
    Results are cached for a long time by default (categories rarely change);
    pass force_refresh=True to bypass the cache.

    Args:
        term: Category name or keyword to search for.
        domain: Amazon marketplace code (US, JP, GB, DE, FR, CA, ...). Default "US".
        force_refresh: Bypass the cache and query Keepa live.
    """
    api_key = _require_api_key()
    results, cache_info = cached_search_categories(api_key, term, domain=domain, force_refresh=force_refresh)
    return {"categories": results, "_cache": cache_info}


@mcp.tool()
def find_candidates(
    keyword: str,
    category_id: Optional[int] = None,
    sales_rank_min: int = 1000,
    sales_rank_max: int = 20000,
    review_count_max: int = 200,
    max_results: int = 50,
    domain: str = "US",
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Coarse, cheap product search via Keepa's Product Finder.

    Filters by keyword (matched against the product title, same as the
    dashboard's manual Finder search) plus sales rank range / max review
    count, with an optional category as additional narrowing - no price
    data yet (call get_product_detail or find_arbitrage_candidates for that).
    Results are cached (default 6h; see KEEPA_CACHE_TTL_FINDER_HOURS) since
    rankings/review counts do not change minute to minute; pass
    force_refresh=True to force a live re-query.

    Args:
        keyword: Search term(s) matched against the product title (space-separated,
            all terms required - e.g. "kitchen gadget").
        category_id: Optional Keepa category id (from search_category) to further
            narrow the keyword search.
        sales_rank_min: Minimum current sales rank (lower rank = better seller).
        sales_rank_max: Maximum current sales rank.
        review_count_max: Maximum current review count.
        max_results: Max ASINs to return (capped at 200 per Keepa Finder page).
        domain: Amazon marketplace code. Default "US".
        force_refresh: Bypass the cache and query Keepa live.
    """
    api_key = _require_api_key()
    try:
        result, cache_info = cached_find_products(
            api_key, domain=domain, keyword=keyword, category_id=category_id,
            sales_rank_min=sales_rank_min, sales_rank_max=sales_rank_max,
            review_count_max=review_count_max, review_count_min=None,
            per_page=max_results, force_refresh=force_refresh,
        )
    except KeepaError as exc:
        status = get_token_status(api_key)
        tokens_left = status["tokens_left"] or 0
        estimated_cost = estimate_finder_cost(max_results)
        return {
            "error": f"Product Finder call failed: {exc}",
            "tokens_left": tokens_left,
            "estimated_cost": estimated_cost,
            "estimated_wait": _wait_estimate(estimated_cost - tokens_left, status["refill_rate_per_minute"]),
        }
    result["_cache"] = cache_info
    return result


@mcp.tool()
def get_product_detail(asin: str, domain: str = "US", force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch full Keepa product detail: current price, 90-day price
    volatility, sales rank, review count, rating, and UPC/EAN identifiers.
    Cached by default (default 6h; see KEEPA_CACHE_TTL_PRODUCT_HOURS) - pass
    force_refresh=True when you want the latest price rather than a cached one.

    Args:
        asin: The ASIN to look up.
        domain: Amazon marketplace code the ASIN belongs to. Default "US".
        force_refresh: Bypass the cache and fetch live from Keepa.
    """
    api_key = _require_api_key()
    products, cache_meta = cached_get_products(api_key, domain=domain, asins=[asin], stats_days=90, force_refresh=force_refresh)
    if not products:
        return {"error": f"No product found for ASIN {asin} in domain {domain}"}
    return _summarize_product(products[0], domain, cache_meta.get(asin))


@mcp.tool()
def find_jp_price(code: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Cross-domain lookup: given a UPC or EAN barcode, find the matching
    product listed on Amazon Japan and its current price. Cached by default
    (default 6h; see KEEPA_CACHE_TTL_PRODUCT_HOURS) - pass force_refresh=True
    to re-check the live price.

    Args:
        code: UPC or EAN of the product (from get_product_detail's upc/ean fields).
        force_refresh: Bypass the cache and look up live on Keepa.
    """
    api_key = _require_api_key()
    products, cache_info = cached_lookup_by_code(api_key, code, domain="JP", force_refresh=force_refresh)
    if not products:
        return {"found": False, "code": code, "_cache": cache_info}
    summary = _summarize_product(products[0], "JP", cache_info)
    summary["found"] = True
    return summary


@mcp.tool()
def find_arbitrage_candidates(
    keyword: str,
    category_id: Optional[int] = None,
    sales_rank_min: int = 1000,
    sales_rank_max: int = 20000,
    review_count_max: int = 200,
    price_diff_min: float = 0.4,
    price_volatility_max: float = 0.2,
    max_candidates: int = 30,
    sell_domain: str = "US",
    usd_to_jpy: Optional[float] = None,
    force_refresh: bool = False,
    wait_for_tokens: bool = False,
) -> Dict[str, Any]:
    """End-to-end search: find products by keyword (plus rank/review-count
    band, and an optional category) on the sell-side marketplace (default
    US), cross-reference their JPY cost on Amazon Japan by ASIN (assumes
    the same ASIN is listed on both marketplaces - not always true, see the
    per-candidate skip reasons), and return only those clearing the
    requested price-gap and price-stability thresholds.

    Pipeline: Product Finder (cheap) -> per-ASIN detail fetch (price/rank/
    reviews/volatility) -> JP cross-domain lookup by the same ASIN -> filter.
    Each live Keepa call costs tokens; results are cached locally (see cache.py)
    so re-running the same/overlapping search is free where the cache is warm.
    Pass force_refresh=True to ignore the cache and pull current prices/ranks
    everywhere (worth doing when your token budget is flush, since prices and
    rankings do genuinely move over time).

    Args:
        keyword: Search term(s) matched against the product title (space-separated,
            all terms required - e.g. "kitchen gadget").
        category_id: Optional Keepa category id (from search_category) to further
            narrow the keyword search.
        sales_rank_min: Minimum current sales rank on the sell side.
        sales_rank_max: Maximum current sales rank on the sell side.
        review_count_max: Maximum current review count on the sell side.
        price_diff_min: Minimum required (sell - cost) / cost, e.g. 0.4 = 40%.
        price_volatility_max: Maximum allowed (max90-min90)/avg90 on the sell side, e.g. 0.2 = 20%.
        max_candidates: Max sell-side ASINs to evaluate (bounds API token usage).
        sell_domain: Marketplace to sell on. Default "US".
        usd_to_jpy: Override the USD->JPY rate used for the price-gap calc
            (defaults to USD_TO_JPY from .env, currently used only when
            sell_domain="US"; the cost side is assumed to be Amazon Japan).
        force_refresh: Bypass the cache everywhere in this pipeline.
        wait_for_tokens: On a low-refill-rate plan, instead of stopping early
            when the live-call budget runs out, sleep and poll the real
            balance (see check_token_balance) until enough tokens have
            regenerated, then keep going until every candidate is evaluated.
            This can block the call for a long time (minutes, possibly tens
            of minutes on a 1 token/min plan) - fine for an unattended script
            like daily_scan.py, but usually leave this False in an
            interactive chat session.
    """
    api_key = _require_api_key()
    rate = usd_to_jpy if usd_to_jpy is not None else settings.usd_to_jpy

    # Free balance check, used only for the per-candidate budget gate below
    # and an informational note - a cache-warm run can still fully succeed
    # even at 0 tokens, so this no longer hard-blocks the pipeline.
    status = get_token_status(api_key)
    tokens_left = status["tokens_left"] or 0
    worst_case_cost = estimate_finder_cost(max_candidates) + 2 * estimate_product_request_cost(max_candidates)
    budget_note = None
    if tokens_left < worst_case_cost:
        if wait_for_tokens:
            budget_note = (
                f"Only {tokens_left} tokens available (worst case needs ~{worst_case_cost} if nothing is "
                "cached); wait_for_tokens=True, so this run will pause and wait for the token bucket to "
                "refill as needed rather than stopping early."
            )
        else:
            budget_note = (
                f"Only {tokens_left} tokens available (worst case needs ~{worst_case_cost} if nothing is "
                "cached); will use cached data where possible and stop early if live calls run out. "
                f"Estimated wait for full budget: {_wait_estimate(worst_case_cost - tokens_left, status['refill_rate_per_minute'])}. "
                "Pass wait_for_tokens=True to wait it out instead of stopping early."
            )
    budget = tokens_left  # tracks *live-call* budget only; cache hits are free and don't touch this

    if wait_for_tokens:
        # Pass current_budget=0 (not the locally tracked `budget`) to force a
        # fresh real-balance check every time - the local estimate can drift
        # from the real Keepa balance (actual token costs aren't always
        # exactly what estimate_finder_cost/estimate_product_request_cost
        # predict), and in wait mode correctness matters more than avoiding
        # one extra free check_token_balance call.
        budget = _wait_for_budget(api_key, estimate_finder_cost(max_candidates), 0)

    try:
        finder, finder_cache_info = cached_find_products(
            api_key, domain=sell_domain, keyword=keyword, category_id=category_id,
            sales_rank_min=sales_rank_min, sales_rank_max=sales_rank_max,
            review_count_max=review_count_max, review_count_min=None,
            per_page=max_candidates, force_refresh=force_refresh,
        )
    except KeepaError as exc:
        return {"candidates": [], "evaluated": 0, "error": f"Product Finder call failed: {exc}", "note": budget_note}
    if not finder_cache_info["hit"]:
        budget -= estimate_finder_cost(max_candidates)

    asins = finder["asins"][:max_candidates]
    if not asins:
        return {"candidates": [], "evaluated": 0, "note": "Product Finder returned no ASINs for these filters."}

    sell_products: List[Dict[str, Any]] = []
    sell_cache_meta: Dict[str, Dict[str, Any]] = {}
    if wait_for_tokens:
        # The token bucket has a hard cap (plan refill-rate x 60 minutes), so
        # a single huge batched request can need more tokens than the bucket
        # can ever hold. Fetch in small chunks instead, waiting for budget
        # before each one - this also means work starts on whatever's
        # already cached/affordable rather than blocking on the whole batch.
        for i in range(0, len(asins), _WAIT_MODE_BATCH_SIZE):
            chunk = asins[i:i + _WAIT_MODE_BATCH_SIZE]
            to_fetch_estimate = sum(1 for a in chunk if force_refresh or not is_product_cached(a, sell_domain))
            if to_fetch_estimate:
                budget = _wait_for_budget(api_key, to_fetch_estimate, 0)  # force real check, see note above
            try:
                chunk_products, chunk_meta = cached_get_products(
                    api_key, domain=sell_domain, asins=chunk, stats_days=90, force_refresh=force_refresh
                )
            except KeepaError as exc:
                return {
                    "candidates": [], "evaluated": 0,
                    "error": f"Product detail fetch failed: {exc}",
                    "note": budget_note,
                }
            budget -= sum(1 for info in chunk_meta.values() if not info["hit"])
            sell_products.extend(chunk_products)
            sell_cache_meta.update(chunk_meta)
    else:
        try:
            sell_products, sell_cache_meta = cached_get_products(
                api_key, domain=sell_domain, asins=asins, stats_days=90, force_refresh=force_refresh
            )
        except KeepaError as exc:
            return {
                "candidates": [], "evaluated": 0,
                "error": f"Product detail fetch failed (likely token budget ran out after the Finder call): {exc}",
                "note": budget_note,
            }
        budget -= sum(1 for info in sell_cache_meta.values() if not info["hit"])

    results = []
    skipped = []
    stopped_early = False
    for product in sell_products:
        asin = product.get("asin")
        sell_summary = _summarize_product(product, sell_domain, sell_cache_meta.get(asin))

        if sell_summary["price"] is None:
            skipped.append({"asin": sell_summary["asin"], "reason": "no current price"})
            continue
        volatility = sell_summary["price_volatility_90d"]
        if volatility is not None and volatility > price_volatility_max:
            skipped.append({"asin": sell_summary["asin"], "reason": f"price volatility {volatility:.2%} exceeds limit"})
            continue

        # ASIN-based cross-domain match: assumes the same ASIN is used on
        # both marketplaces. Note this does NOT hold in general - ASINs are
        # assigned per-marketplace, so many genuinely-dual-market products
        # (especially non-brand-registered ones) use a different ASIN in
        # each catalog and will not be found this way. When Keepa has no
        # such ASIN in the JP catalog it still returns a near-empty stub
        # (no price/stats), which the price check below skips.
        needs_live_call = force_refresh or not is_product_cached(asin, "JP")
        if needs_live_call and wait_for_tokens:
            # Force a real-balance check every time (current_budget=0) rather
            # than trusting the locally tracked estimate - see note above.
            budget = _wait_for_budget(api_key, estimate_product_request_cost(1), 0)
        elif needs_live_call and budget < estimate_product_request_cost(1):
            skipped.append({"asin": sell_summary["asin"], "reason": "stopped early: Keepa token budget ran out"})
            stopped_early = True
            continue

        try:
            jp_matches, jp_cache_meta = cached_get_products(api_key, domain="JP", asins=[asin], stats_days=90, force_refresh=force_refresh)
        except KeepaError as exc:
            skipped.append({"asin": sell_summary["asin"], "reason": f"JP lookup failed (token budget likely exhausted): {exc}"})
            stopped_early = True
            continue
        jp_cache_info = jp_cache_meta.get(asin, {"hit": False})
        if not jp_cache_info["hit"]:
            budget -= estimate_product_request_cost(1)

        if not jp_matches:
            skipped.append({"asin": sell_summary["asin"], "reason": "ASIN not found in Amazon Japan catalog"})
            continue

        jp_summary = _summarize_product(jp_matches[0], "JP", jp_cache_info)
        if jp_summary["price"] is None:
            skipped.append({"asin": sell_summary["asin"], "reason": "no current price for this ASIN on Amazon Japan (may not exist in the JP catalog)"})
            continue

        diff_rate = analysis.price_diff_rate(
            sell_summary["price"], sell_domain, jp_summary["price"], "JP", rate
        )
        if diff_rate is None or diff_rate < price_diff_min:
            skipped.append({
                "asin": sell_summary["asin"],
                "reason": f"price diff rate {diff_rate:.2%} below {price_diff_min:.0%}" if diff_rate is not None else "could not compute price diff rate",
            })
            continue

        results.append({
            "sell": sell_summary,
            "cost": jp_summary,
            "price_diff_rate": diff_rate,
            "price_volatility_90d": volatility,
            "usd_to_jpy_used": rate if sell_domain.upper() == "US" else None,
        })

    results.sort(key=lambda r: r["price_diff_rate"], reverse=True)
    return {
        "candidates": results,
        "evaluated": len(sell_products),
        "matched": len(results),
        "skipped": skipped,
        "stopped_early_for_tokens": stopped_early,
        "note": budget_note,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
