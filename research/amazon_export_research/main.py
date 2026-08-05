import csv
import gzip
import json
import os
import urllib.parse
import urllib.request
try:
    import requests
    _REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    _REQUESTS_AVAILABLE = False
from typing import Dict, List, Optional

RESULT_CSV = "research_result.csv"
EXCHANGE_RATE = 150
AMAZON_FEE_RATE = 0.15
FBA_AND_SHIPPING = 1500
US_DOMAIN = 1
JP_DOMAIN = 5
SEARCH_URL = "https://api.keepa.com/search"
PRODUCT_URL = "https://api.keepa.com/product"
DEFAULT_MIN_PRICE_USD = 30.0


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        alt_path = os.path.join(script_dir, path)
        if os.path.exists(alt_path):
            path = alt_path
        else:
            return

    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def _load_api_key() -> str:
    api_key = os.getenv("KEEPA_API_KEY", "").strip()
    if api_key:
        return api_key

    _load_dotenv()
    api_key = os.getenv("KEEPA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("KEEPA_API_KEY is required in the .env file or environment.")
    return api_key


def _keepa_request(url: str, params: Dict) -> Dict:
    if _REQUESTS_AVAILABLE:
        session = requests.Session()
        session.trust_env = False

        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()

        try:
            return resp.json()
        except ValueError:
            body = resp.content
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
            return json.loads(body.decode("utf-8", errors="ignore"))

    # Fallback: remove proxy-related env vars and use urllib without proxy
    for key in list(os.environ.keys()):
        lower_key = key.lower()
        if 'proxy' in lower_key or lower_key in ('grpc', 'rsync'):
            os.environ.pop(key, None)

    query = urllib.parse.urlencode(params)
    request_url = f"{url}?{query}"
    # debug: show request URL and any proxies urllib sees
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    print("DEBUG: urllib request_url=", request_url)
    print("DEBUG: urllib.getproxies()=", proxies)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request_url, timeout=30) as response:
        body = response.read()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        if "gzip" in content_encoding:
            body = gzip.decompress(body)

    return json.loads(body.decode("utf-8", errors="ignore"))


def _normalize_price(cents: Optional[int]) -> Optional[float]:
    if cents is None or cents <= 0:
        return None
    return cents / 100.0


def agent1_generate_keywords(category: str) -> List[str]:
    category = category.strip().lower()
    mapping = {
        "フィギュア": ["Nendoroid", "S.H.Figuarts", "Figma", "Kotobukiya", "POP! Vinyl"],
        "文具": ["Pilot Frixion", "Uni-ball Signo", "Tombow Mono", "Zebra Sarasa", "Muji pen"],
        "ゴルフ": ["Titleist golf ball", "TaylorMade driver", "Callaway golf club", "PING iron", "Nike golf glove"],
        "figure": ["Nendoroid", "S.H.Figuarts", "Figma", "Kotobukiya", "POP! Vinyl"],
        "stationery": ["Pilot Frixion", "Uni-ball Signo", "Tombow Mono", "Zebra Sarasa", "Muji pen"],
        "golf": ["Titleist golf ball", "TaylorMade driver", "Callaway golf club", "PING iron", "Nike golf glove"],
    }
    return mapping.get(category, [category or "Nendoroid"])


def _extract_asins_from_search_response(data: Dict, min_price_usd: float) -> List[str]:
    asins: List[str] = []
    for product in data.get("products", []):
        asin = product.get("asin")
        buy_box_price = _normalize_price(product.get("buyBoxPrice"))
        list_price = _normalize_price(product.get("listPrice"))
        price = buy_box_price or list_price
        if asin and price and price >= min_price_usd:
            asins.append(asin)
    return asins


def agent2_search_us_keepa(keywords: List[str], api_key: str, min_price_usd: float) -> List[str]:
    unique_asins = set()
    for keyword in keywords:
        params = {
            "key": api_key,
            "domain": US_DOMAIN,
            "type": "product",
            "term": keyword,
            "buyBoxMin": int(min_price_usd * 100),
            "availabilityAmazon": -1,
            "offers": 1,
            "history": 0,
        }
        response = _keepa_request(SEARCH_URL, params)
        print(f"DEBUG: keyword '{keyword}' -> products={len(response.get('products', []))}")
        asins = _extract_asins_from_search_response(response, min_price_usd)
        unique_asins.update(asins)

    return sorted(unique_asins)


def _get_keepa_product(asin: str, domain: int, api_key: str) -> Optional[Dict]:
    params = {
        "key": api_key,
        "domain": domain,
        "asin": asin,
        "history": 0,
    }
    response = _keepa_request(PRODUCT_URL, params)
    products = response.get("products") or []
    return products[0] if products else None


def _extract_keepa_price(product: Optional[Dict]) -> Optional[float]:
    if not product:
        return None
    buy_box_price = _normalize_price(product.get("buyBoxPrice"))
    list_price = _normalize_price(product.get("listPrice"))
    return buy_box_price or list_price


def agent3_check_jp_keepa(asins: List[str], api_key: str) -> List[Dict]:
    results: List[Dict] = []
    for asin in asins:
        us_product = _get_keepa_product(asin, US_DOMAIN, api_key)
        jp_product = _get_keepa_product(asin, JP_DOMAIN, api_key)

        us_price = _extract_keepa_price(us_product)
        jp_price = _extract_keepa_price(jp_product)
        title = jp_product.get("title") if jp_product and jp_product.get("title") else us_product.get("title") if us_product else "Unknown"

        if us_price is None or jp_price is None:
            print(f"SKIP {asin}: US price or JP price is missing.")
            continue

        us_price_jpy = us_price * EXCHANGE_RATE
        amazon_fee = us_price_jpy * AMAZON_FEE_RATE
        profit = us_price_jpy - jp_price - amazon_fee - FBA_AND_SHIPPING

        results.append(
            {
                "asin": asin,
                "title": title,
                "us_price": us_price,
                "jp_price": jp_price,
                "expected_profit": round(profit, 2),
            }
        )

    if results:
        with open(RESULT_CSV, "w", newline="", encoding="utf-8-sig") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=["asin", "title", "us_price", "jp_price", "expected_profit"],
            )
            writer.writeheader()
            writer.writerows(results)

        print("\n=== リサーチ結果 ===")
        for item in results:
            print(
                f"ASIN: {item['asin']} | {item['title']} | US=${item['us_price']:.2f} | JP=¥{item['jp_price']:.0f} | 想定純利益=¥{item['expected_profit']:.0f}"
            )
        print(f"\nSaved research results to {RESULT_CSV}\n")
    else:
        print("有効な商品データが見つかりませんでした。")

    return results


def main() -> None:
    api_key = _load_api_key()
    category = os.getenv("SEARCH_CATEGORY", "フィギュア")
    try:
        min_price_usd = float(os.getenv("MIN_PRICE_USD", str(DEFAULT_MIN_PRICE_USD)))
    except Exception:
        min_price_usd = DEFAULT_MIN_PRICE_USD

    print(f"Search category: {category}")
    keywords = agent1_generate_keywords(category)
    print(f"Generated keywords: {keywords}")

    asins = agent2_search_us_keepa(keywords, api_key, min_price_usd)
    print(f"Found {len(asins)} ASIN(s) from US Keepa search.")

    if not asins:
        print("条件に合うASINが見つかりませんでした。")
        return

    agent3_check_jp_keepa(asins, api_key)


if __name__ == "__main__":
    main()
