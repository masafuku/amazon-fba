import gzip
import re
from pathlib import Path

path = Path('/tmp/amazon_us_pilot.html')
raw = path.read_bytes()
if raw.startswith(b'\x1f\x8b\x08'):
    html = gzip.decompress(raw).decode('utf-8', errors='ignore')
else:
    html = raw.decode('utf-8', errors='ignore')

# Find first 10 search result blocks
block_pattern = re.compile(r'(<div[^>]+role=["\']listitem["\'][^>]+data-component-type=["\']s-search-result["\'][^>]*>.*?</div>\s*</div>)', re.S)
blocks = block_pattern.findall(html)
print('blocks found', len(blocks))
for i, block in enumerate(blocks[:10]):
    title_match = re.search(r'<span[^>]+class=["\']([^"\']*a-size-[^"\']*)["\'][^>]*>(.*?)</span>', block, re.S)
    title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip() if title_match else None
    price_match = re.search(r'(\$\s*[0-9,]+(?:\.[0-9]{2})?)', block)
    asin_match = re.search(r'data-asin=["\']([A-Z0-9]{10})["\']', block)
    print('--- block', i, '---')
    print('asin', asin_match.group(1) if asin_match else 'none')
    print('title_class', title_match.group(1) if title_match else 'none')
    print('title', title)
    print('price', price_match.group(0) if price_match else 'none')
    print('block snippet:', re.sub(r'\s+', ' ', block[:400]))
    print()