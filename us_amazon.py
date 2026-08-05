import re
import urllib.request
import urllib.parse
from typing import List, Dict

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_us_search_url(keyword: str, category: str = "All") -> str:
    base = "https://www.amazon.com/s"
    params = {"k": keyword}
    if category and category != "All":
        params["i"] = category
    return base + "?" + urllib.parse.urlencode(params)


def parse_asins_from_search(html: str, max_results: int = 20) -> List[Dict]:
    items: List[Dict] = []
    pattern = re.compile(r"<div[^>]+data-asin=['\"](?P<asin>[A-Z0-9]{10})['\"][^>]*>(?P<body>.*?)</div>", re.S)
    title_pattern = re.compile(r"<span[^>]*class=['\"][^\"']*a-size-medium[^\"']*['\"][^>]*>(?P<title>.*?)</span>", re.S)

    for match in pattern.finditer(html):
        if len(items) >= max_results:
            break

        asin = match.group("asin")
        body = match.group("body")
        title_match = title_pattern.search(body)
        title = title_match.group("title") if title_match else None
        if title:
            title = re.sub(r"<[^>]+>", "", title).strip()
        if asin and title:
            items.append({"asin": asin, "title": title})

    return items


def search_us_asins(keyword: str, category: str = "All", max_results: int = 20) -> List[Dict]:
    url = build_us_search_url(keyword, category)
    request = urllib.request.Request(url, headers=HEADERS)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        html = response.read().decode("utf-8", errors="ignore")
    return parse_asins_from_search(html, max_results=max_results)
