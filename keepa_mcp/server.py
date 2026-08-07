"""MCP server exposing Keepa-backed tools for finding Japan -> North America
Amazon arbitrage candidates.

Run directly for local testing:
    python -m keepa_mcp.server

Registered as a stdio MCP server in ../.mcp.json under the name "keepa".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from . import analysis
from .config import settings
from .keepa_client import KeepaError, find_products, get_products, lookup_by_code, search_categories

mcp = FastMCP("keepa-arbitrage-finder")


def _require_api_key() -> str:
    if not settings.keepa_api_key:
        raise KeepaError(
            "KEEPA_API_KEY is not set. Add it to the repository's .env file "
            "(see .env.example) and restart the MCP server."
        )
    return settings.keepa_api_key


def _summarize_product(product: Dict[str, Any], domain: str) -> Dict[str, Any]:
    asin = product.get("asin")
    return {
        "asin": asin,
        "title": product.get("title"),
        "brand": product.get("brand"),
        "domain": domain.upper(),
        "price": analysis.current_price(product, domain),
        "currency": None,  # filled in by caller when known
        "sales_rank": analysis.sales_rank(product),
        "review_count": analysis.review_count(product),
        "rating": analysis.rating(product),
        "price_volatility_90d": analysis.price_volatility_ratio(product, domain),
        "upc": (product.get("upcList") or [None])[0],
        "ean": (product.get("eanList") or [None])[0],
        "url": analysis.product_url(asin, domain),
    }


@mcp.tool()
def search_category(term: str, domain: str = "US") -> List[Dict[str, Any]]:
    """Search Keepa's category tree by name and return matching category ids.

    Use this first to resolve a category name (e.g. "Kitchen Utensils & Gadgets")
    to the numeric category_id needed by find_candidates / find_arbitrage_candidates.
    Category names are in the target marketplace's language (English for domain=US).

    Args:
        term: Category name or keyword to search for.
        domain: Amazon marketplace code (US, JP, GB, DE, FR, CA, ...). Default "US".
    """
    api_key = _require_api_key()
    return search_categories(api_key, term, domain=domain)


@mcp.tool()
def find_candidates(
    category_id: int,
    sales_rank_min: int = 1000,
    sales_rank_max: int = 20000,
    review_count_max: int = 200,
    max_results: int = 50,
    domain: str = "US",
) -> Dict[str, Any]:
    """Coarse, cheap product search via Keepa's Product Finder.

    Filters only by category / sales rank range / max review count - no price
    data yet (call get_product_detail or find_arbitrage_candidates for that).

    Args:
        category_id: Keepa category id (from search_category).
        sales_rank_min: Minimum current sales rank (lower rank = better seller).
        sales_rank_max: Maximum current sales rank.
        review_count_max: Maximum current review count.
        max_results: Max ASINs to return (capped at 200 per Keepa Finder page).
        domain: Amazon marketplace code. Default "US".
    """
    api_key = _require_api_key()
    result = find_products(
        api_key,
        domain=domain,
        category_id=category_id,
        sales_rank_min=sales_rank_min,
        sales_rank_max=sales_rank_max,
        review_count_max=review_count_max,
        per_page=max_results,
    )
    return result


@mcp.tool()
def get_product_detail(asin: str, domain: str = "US") -> Dict[str, Any]:
    """Fetch full Keepa product detail: current price, 90-day price
    volatility, sales rank, review count, rating, and UPC/EAN identifiers.

    Args:
        asin: The ASIN to look up.
        domain: Amazon marketplace code the ASIN belongs to. Default "US".
    """
    api_key = _require_api_key()
    products = get_products(api_key, domain=domain, asins=[asin], stats_days=90)
    if not products:
        return {"error": f"No product found for ASIN {asin} in domain {domain}"}
    summary = _summarize_product(products[0], domain)
    summary["currency"] = _currency_for(domain)
    return summary


@mcp.tool()
def find_jp_price(code: str) -> Dict[str, Any]:
    """Cross-domain lookup: given a UPC or EAN barcode, find the matching
    product listed on Amazon Japan and its current price.

    Args:
        code: UPC or EAN of the product (from get_product_detail's upc/ean fields).
    """
    api_key = _require_api_key()
    products = lookup_by_code(api_key, code, domain="JP")
    if not products:
        return {"found": False, "code": code}
    summary = _summarize_product(products[0], "JP")
    summary["currency"] = "JPY"
    summary["found"] = True
    return summary


def _currency_for(domain: str) -> str:
    from .keepa_client import CURRENCY_CODE
    return CURRENCY_CODE.get(domain.upper(), "?")


@mcp.tool()
def find_arbitrage_candidates(
    category_id: int,
    sales_rank_min: int = 1000,
    sales_rank_max: int = 20000,
    review_count_max: int = 200,
    price_diff_min: float = 0.4,
    price_volatility_max: float = 0.2,
    max_candidates: int = 30,
    sell_domain: str = "US",
    usd_to_jpy: Optional[float] = None,
) -> Dict[str, Any]:
    """End-to-end search: find products in a category/rank/review-count band
    on the sell-side marketplace (default US), cross-reference their JPY cost
    on Amazon Japan by UPC/EAN, and return only those clearing the requested
    price-gap and price-stability thresholds.

    Pipeline: Product Finder (cheap) -> per-ASIN detail fetch (price/UPC/rank/
    reviews/volatility) -> JP cross-domain price lookup by UPC/EAN -> filter.
    Each step costs Keepa API tokens; keep max_candidates modest while tuning.

    Args:
        category_id: Keepa category id on the sell-side marketplace (from search_category).
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
    """
    api_key = _require_api_key()
    rate = usd_to_jpy if usd_to_jpy is not None else settings.usd_to_jpy

    finder = find_products(
        api_key,
        domain=sell_domain,
        category_id=category_id,
        sales_rank_min=sales_rank_min,
        sales_rank_max=sales_rank_max,
        review_count_max=review_count_max,
        per_page=max_candidates,
    )
    asins = finder["asins"][:max_candidates]
    if not asins:
        return {"candidates": [], "evaluated": 0, "note": "Product Finder returned no ASINs for these filters."}

    sell_products = get_products(api_key, domain=sell_domain, asins=asins, stats_days=90)

    results = []
    skipped = []
    for product in sell_products:
        sell_summary = _summarize_product(product, sell_domain)
        sell_summary["currency"] = _currency_for(sell_domain)
        code = sell_summary["upc"] or sell_summary["ean"]

        if sell_summary["price"] is None:
            skipped.append({"asin": sell_summary["asin"], "reason": "no current price"})
            continue
        volatility = sell_summary["price_volatility_90d"]
        if volatility is not None and volatility > price_volatility_max:
            skipped.append({"asin": sell_summary["asin"], "reason": f"price volatility {volatility:.2%} exceeds limit"})
            continue
        if not code:
            skipped.append({"asin": sell_summary["asin"], "reason": "no UPC/EAN to cross-reference against Amazon Japan"})
            continue

        jp_matches = lookup_by_code(api_key, code, domain="JP")
        if not jp_matches:
            skipped.append({"asin": sell_summary["asin"], "reason": f"no Amazon Japan listing found for code {code}"})
            continue

        jp_summary = _summarize_product(jp_matches[0], "JP")
        jp_summary["currency"] = "JPY"
        if jp_summary["price"] is None:
            skipped.append({"asin": sell_summary["asin"], "reason": "matched JP listing has no current price"})
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
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
