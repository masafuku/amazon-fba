import gzip
import re
from pathlib import Path

path = Path('/tmp/amazon_us_pilot.html')
raw = path.read_bytes()
if raw.startswith(b'\x1f\x8b\x08'):
    html = gzip.decompress(raw).decode('utf-8', errors='ignore')
else:
    html = raw.decode('utf-8', errors='ignore')

print('len', len(html))
print('data-asin count:', html.count('data-asin='))
print('a-size-medium count:', html.count('a-size-medium'))
print('first /dp asins:', re.findall(r'/dp/([A-Z0-9]{10})', html)[:10])
print('first Pilot index:', html.find('Pilot'))
print('first Frixion index:', html.find('Frixion'))
print('--- first 10 data-asin snippets ---')
for i, m in enumerate(re.finditer(r'data-asin=["\']([A-Z0-9]{10})["\']', html)):
    if i >= 10:
        break
    start = max(0, m.start() - 120)
    end = min(len(html), m.end() + 220)
    snippet = html[start:end]
    print(i, 'ASIN', m.group(1), 'snippet:', re.sub(r'\s+', ' ', snippet))

print('--- first 10 title spans ---')
for i, m in enumerate(re.finditer(r'<span[^>]+class=["\'][^"\']*a-size-medium[^"\']*["\'][^>]*>(.*?)</span>', html, re.S)):
    if i >= 10:
        break
    text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    print(i, 'text=', text)

print('--- first 10 price occurrences ---')
for i, m in enumerate(re.finditer(r'\$\s*[0-9,.]+|¥\s*[0-9,]+|[0-9,]+\s*円', html)):
    if i >= 20:
        break
    print(i, m.group(0), 'context=', re.sub(r'\s+', ' ', html[max(0, m.start()-50):m.end()+50]))
