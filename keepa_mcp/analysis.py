"""Pure helper functions for turning raw Keepa product payloads into the
metrics the arbitrage search cares about. No network calls in this module.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .keepa_client import CsvType, CURRENCY_DIVISOR, NO_DATA_SENTINELS


def _stat_value(stats: Dict[str, Any], field: str, type_index: int) -> Optional[float]:
    arr = stats.get(field)
    if not arr or type_index >= len(arr):
        return None
    val = arr[type_index]
    if val is None or val in NO_DATA_SENTINELS:
        return None
    return val


def _stat_pair_value(stats: Dict[str, Any], field: str, type_index: int) -> Optional[float]:
    """For fields like `min`/`max`/`minInInterval`/`maxInInterval` which are
    arrays of [time, value] pairs indexed by csv type."""
    arr = stats.get(field)
    if not arr or type_index >= len(arr):
        return None
    pair = arr[type_index]
    if not pair or len(pair) < 2:
        return None
    val = pair[1]
    if val is None or val in NO_DATA_SENTINELS:
        return None
    return val


def current_price(product: Dict[str, Any], domain: str) -> Optional[float]:
    """Best-effort current landed price (buy box w/ shipping, falling back to
    marketplace new / Amazon), converted to the marketplace's major currency unit."""
    stats = product.get("stats") or {}
    divisor = CURRENCY_DIVISOR.get(domain.upper(), 100)
    for type_index in (CsvType.BUY_BOX_SHIPPING, CsvType.NEW, CsvType.AMAZON):
        raw = _stat_value(stats, "current", type_index)
        if raw is not None and raw >= 0:
            return raw / divisor
    return None


def sales_rank(product: Dict[str, Any]) -> Optional[int]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.SALES)
    return int(val) if val is not None else None


def review_count(product: Dict[str, Any]) -> Optional[int]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.COUNT_REVIEWS)
    return int(val) if val is not None else None


def rating(product: Dict[str, Any]) -> Optional[float]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.RATING)
    return val / 10 if val is not None else None


def monthly_sold(product: Dict[str, Any]) -> Optional[int]:
    """Keepa's own bucketed estimate of units sold in the past ~30 days
    (Amazon's "X+ bought in past month" badge data). Already present on the
    standard /product response we fetch - no extra token cost to read it."""
    val = product.get("monthlySold")
    return int(val) if isinstance(val, (int, float)) and val >= 0 else None


# Keepa uses these sentinel seller ids in buyBoxSellerIdHistory to mean
# "no seller qualified for the buy box" (-1) / "a brand-new, unknown seller"
# (-2) - neither is a real, queryable seller id.
_NO_SELLER_SENTINELS = ("-1", "-2")


def current_buy_box_seller_id(product: Dict[str, Any]) -> Optional[str]:
    """Most recent real seller id from buyBoxSellerIdHistory (flat array:
    [keepaMinutes, sellerId, keepaMinutes, sellerId, ...], most recent last).
    Only present when the product was fetched with buybox=1 (see
    keepa_client.get_products(include_buybox=True)) - this is the expensive
    (5 tokens/product) call, so only do this for candidates worth the cost
    (e.g. already-qualified ones, for seller-based expansion)."""
    history = product.get("buyBoxSellerIdHistory")
    if not history or not isinstance(history, list):
        return None
    # 末尾から遡って、-1/-2でない実在のsellerIdを探す(直近の有効な入札者)。
    for i in range(len(history) - 1, 0, -2):
        seller_id = history[i]
        if seller_id and seller_id not in _NO_SELLER_SENTINELS:
            return seller_id
    return None


def distinct_seller_ids(product: Dict[str, Any], exclude_amazon: bool = True) -> List[str]:
    """All distinct sellerIds currently offering this ASIN (not just the buy
    box winner) - Amazon's "Other sellers on Amazon" list, in effect. Only
    present when the product was fetched with offers_limit set (see
    keepa_client.get_products(offers_limit=N)); each real dict in the
    `offers` array has its own `sellerId`. Order is preserved, first-seen
    (roughly Amazon's own offer ranking), duplicates removed.

    exclude_amazon=True (default) drops offers where isAmazon is true -
    Amazon itself isn't a "seller to mine" for this pipeline's purposes.
    """
    offers = product.get("offers")
    if not offers or not isinstance(offers, list):
        return []
    seen = set()
    seller_ids: List[str] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if exclude_amazon and offer.get("isAmazon"):
            continue
        seller_id = offer.get("sellerId")
        if seller_id and seller_id not in seen:
            seen.add(seller_id)
            seller_ids.append(seller_id)
    return seller_ids


def price_volatility_ratio(product: Dict[str, Any], domain: str) -> Optional[float]:
    """(max - min) / avg over the interval the product was fetched with
    (call get_products(..., stats_days=90) so this reflects the last 90 days).
    Returns None if there isn't enough price history to compute it."""
    stats = product.get("stats") or {}
    for type_index in (CsvType.BUY_BOX_SHIPPING, CsvType.NEW, CsvType.AMAZON):
        lo = _stat_pair_value(stats, "minInInterval", type_index)
        hi = _stat_pair_value(stats, "maxInInterval", type_index)
        avg = _stat_value(stats, "avg", type_index)
        if lo is not None and hi is not None and avg:
            return (hi - lo) / avg
    return None


def package_weight_kg(product: Dict[str, Any]) -> Optional[float]:
    """Package weight in kg, used for international-shipping cost estimates.
    Keepa returns `packageWeight` in grams; falls back to `itemWeight` if the
    package figure isn't present. Returns None if neither is available (the
    caller should fall back to a manual/default estimate in that case)."""
    for field in ("packageWeight", "itemWeight"):
        raw = product.get(field)
        if raw is not None and raw > 0:
            return round(raw / 1000, 3)
    return None


def product_image_url(product: Dict[str, Any]) -> Optional[str]:
    """Primary product thumbnail URL.

    `imagesCSV` (when present) is a semicolon-separated string of bare
    Amazon image ids. `images` (the fallback when imagesCSV is absent) is
    structured instead - a list of per-image dicts with size-variant keys
    like "l" (large) / "m" (medium) filenames, NOT a list of plain
    strings - so it must not be naively str()'d (that stringifies the
    whole dict into the URL)."""
    images_csv = product.get("imagesCSV")
    if images_csv:
        image_value = str(images_csv).split(";")[0].strip()
    else:
        raw_images = product.get("images")
        image_value = ""
        if isinstance(raw_images, list) and raw_images:
            first = raw_images[0]
            if isinstance(first, dict):
                image_value = str(first.get("l") or first.get("hiRes") or first.get("m") or "").strip()
            else:
                image_value = str(first).strip()
        elif isinstance(raw_images, str):
            image_value = raw_images.split(";")[0].strip()

    if not image_value:
        return None
    if image_value.startswith(("http://", "https://")):
        return image_value
    # imagesCSV entries are bare ids with no extension; the "l"/"m" filenames
    # from `images` already end in .jpg - don't double it up.
    suffix = "" if image_value.lower().endswith((".jpg", ".jpeg", ".png", ".gif")) else ".jpg"
    return f"https://images-na.ssl-images-amazon.com/images/I/{image_value}{suffix}"


def referral_fee_percent(product: Dict[str, Any]) -> Optional[float]:
    """Amazon referral (selling) fee as a percentage of price, e.g. 15.01
    means 15.01%. Category-specific (ranges roughly 8-45% on Amazon), so
    prefer this over a flat assumption whenever Keepa has it."""
    val = product.get("referralFeePercentage")
    if val is None or val < 0:
        return None
    return val


def fba_pickpack_fee(product: Dict[str, Any], domain: str) -> Optional[float]:
    """FBA pick & pack fee in the marketplace's major currency unit
    (e.g. USD for domain="US"). Size/weight-tier dependent on Amazon's
    side, so this is Keepa's own per-product estimate, not a flat rate."""
    fees = product.get("fbaFees") or {}
    raw = fees.get("pickAndPackFee")
    if raw is None or raw < 0:
        return None
    divisor = CURRENCY_DIVISOR.get(domain.upper(), 100)
    return round(raw / divisor, 2)


def primary_code(product: Dict[str, Any]) -> Optional[str]:
    """Primary UPC, falling back to EAN, used to match the same physical
    product across Amazon marketplaces (ASINs differ per-domain)."""
    upc_list = product.get("upcList") or []
    if upc_list:
        return str(upc_list[0])
    ean_list = product.get("eanList") or []
    if ean_list:
        return str(ean_list[0])
    return None


def price_diff_rate(sell_price_local: float, sell_domain: str, cost_price_local: float,
                     cost_domain: str, usd_to_jpy: float) -> Optional[float]:
    """(sell - cost) / cost, with both prices normalized to JPY.

    sell_domain/cost_domain are Keepa domain codes ("US", "JP", ...).
    Only USD<->JPY conversion is implemented (matches the Japan->US arbitrage
    use case); pass already-JPY prices for cost_domain="JP".
    """
    if cost_price_local is None or cost_price_local <= 0 or sell_price_local is None:
        return None

    def to_jpy(amount: float, domain: str) -> float:
        if domain.upper() == "JP":
            return amount
        if domain.upper() == "US":
            return amount * usd_to_jpy
        raise ValueError(f"price_diff_rate only supports US/JP domains, got '{domain}'")

    sell_jpy = to_jpy(sell_price_local, sell_domain)
    cost_jpy = to_jpy(cost_price_local, cost_domain)
    if cost_jpy <= 0:
        return None
    return (sell_jpy - cost_jpy) / cost_jpy


def product_url(asin: str, domain: str) -> Optional[str]:
    if not asin:
        return None
    host = {"US": "amazon.com", "JP": "amazon.co.jp", "GB": "amazon.co.uk",
            "DE": "amazon.de", "FR": "amazon.fr", "CA": "amazon.ca"}.get(domain.upper())
    if not host:
        return None
    return f"https://www.{host}/dp/{asin}"
