import re
import urllib.request
from urllib.error import HTTPError, URLError

url = (
    "https://www.amazon.com/Pilot-SFL-60SL-6C-Highlighter-Frixion-Light/dp/B00N7IWD8E/"
    "ref=sr_1_1?crid=1NQJF90BR8FA8&dib=eyJ2IjoiMSJ9.uiHLzCfK_IOhlxxDMJsozg.N3GwcNckciKdniQq68LFJuh0C79Mey2MCKdNMajGKGU"
    "&dib_tag=se&keywords=frixion%2BSFL60SL6C&qid=1785643183&sprefix=frixion%2B%2Caps%2C287&sr=8-1&th=1"
)

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

req = urllib.request.Request(url, headers=headers)

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        raw = resp.read()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("utf-8", errors="ignore")
        print("status", resp.status)
        print("len", len(html))
        print("contains asin", "B00N7IWD8E" in html)
        print("contains Amazon.com", "Amazon.com" in html)
        title = re.search(r"<span[^>]+id=[\"']productTitle[\"'][^>]*>(.*?)</span>", html, re.S)
        price = re.search(r"id=[\"']priceblock_ourprice[\"'][^>]*>\s*\$([0-9.,]+)", html)
        deal = re.search(r"id=[\"']priceblock_dealprice[\"'][^>]*>\s*\$([0-9.,]+)", html)
        json_price = re.search(r'"price":"([0-9.]+)"', html)
        print("title", title.group(1).strip() if title else None)
        print("ourprice", price.group(1) if price else None)
        print("dealprice", deal.group(1) if deal else None)
        print("json_price", json_price.group(1) if json_price else None)
except HTTPError as exc:
    print("HTTPError", exc.code)
    try:
        print(exc.read().decode("utf-8", errors="ignore")[:1000])
    except Exception:
        pass
except URLError as exc:
    print("URLError", exc)
