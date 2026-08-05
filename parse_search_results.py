import gzip
import re
from pathlib import Path

SEARCH_PAGES = [
    ('US', '/tmp/amazon_us_pilot.html'),
    ('JP', '/tmp/amazon_jp_pilot.html'),
]

asin_pattern = re.compile(r'<div[^>]+data-asin=["\']([A-Z0-9]{10})["\'][^>]*>(.*?)</div>', re.S)
title_pattern = re.compile(r'<span[^>]*class=["\'][^"\']*a-size-medium[^"\']*["\'][^>]*>(.*?)</span>', re.S)
price_pattern = re.compile(r'(?:\$\s*[0-9,.]+|¥\s*[0-9,]+|[0-9,]+\s*円)')

for label, path_str in SEARCH_PAGES:
    path = Path(path_str)
    raw = path.read_bytes()
    if raw.startswith(b'\x1f\x8b\x08'):
        html = gzip.decompress(raw).decode('utf-8', errors='ignore')
    else:
        html = raw.decode('utf-8', errors='ignore')

    items = []
    for match in asin_pattern.finditer(html):
        asin = match.group(1)
        body = match.group(2)
        title_match = title_pattern.search(body)
        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else None
        if title:
            price_match = price_pattern.search(body)
            price = price_match.group(0) if price_match else None
            items.append((asin, title, price))

    print('===', label, 'search results ===')
    print('total items parsed:', len(items))
    for asin, title, price in items[:20]:
        print(asin, '|', title, '|', price)
    print()