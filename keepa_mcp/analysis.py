"""Pure helper functions for turning raw Keepa product payloads into the
metrics the arbitrage search cares about. No network calls in this module.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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
