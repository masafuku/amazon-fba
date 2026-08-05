import gzip
import json
import urllib.parse
import urllib.request
from typing import List, Dict

KEEPA_API_URL = "https://api.keepa.com/product"


def fetch_keepa_products(api_key: str, asins: List[str]) -> List[Dict]:
    if not asins:
        return []

    params = {
        "key": api_key,
        "domain": 1,
        "asin": ",".join(asins),
        "buybox": 1,
        "offers": 20,
        "stats": 1,
        "history": 0,
    }
    url = KEEPA_API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with opener.open(request, timeout=30) as response:
        body = response.read()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        if "gzip" in content_encoding:
            body = gzip.decompress(body)

    data = json.loads(body.decode("utf-8", errors="ignore"))
    if "products" not in data:
        raise ValueError("Keepa API response missing products field")

    return data["products"]


def map_keepa_product(product: Dict) -> Dict:
    buybox = product.get("buyBoxPrice")
    list_price = product.get("listPrice")
    price = None
    if buybox and buybox > 0:
        price = buybox / 100
    elif list_price and list_price > 0:
        price = list_price / 100

    sales_rank = None
    stats = product.get("stats") or {}
    current = stats.get("current") or {}
    sales_rank = current.get("offerSalesRank") or current.get("salesRank")

    return {
        "asin": product.get("asin"),
        "title": product.get("title"),
        "brand": product.get("brand"),
        "category": product.get("categoryTree", []),
        "us_price": price,
        "is_fba": any(offer.get("isFBA") for offer in product.get("offers", [])) if product.get("offers") else False,
        "product_url": f"https://www.amazon.com/dp/{product.get('asin')}" if product.get("asin") else None,
        "sales_rank": sales_rank,
        "product_data": product,
    }
