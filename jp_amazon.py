import re
import urllib.request
from typing import Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
}


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", text).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def search_jp_by_asin(asin: str) -> Optional[float]:
    url = f"https://www.amazon.co.jp/dp/{asin}"
    request = urllib.request.Request(url, headers=HEADERS)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    # try to capture common Japanese price display patterns
    price_patterns = [
        r"¥\s*([0-9,]+)",
        r"([0-9,]+)\s*円",
    ]
    for pattern in price_patterns:
        match = re.search(pattern, html)
        if match:
            value = parse_price(match.group(0))
            if value is not None:
                return value

    return None
