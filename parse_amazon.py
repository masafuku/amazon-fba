import re
from pathlib import Path

us_path = Path('/tmp/amazon_us_pilot.html')
jp_path = Path('/tmp/amazon_jp_pilot.html')

for path, label in [(us_path, 'US'), (jp_path, 'JP')]:
    print('---', label, 'page ---')
    html = path.read_text(encoding='utf-8', errors='ignore')
    asins = re.findall(r'data-asin=["\']([A-Z0-9]{10})["\']', html)
    print('ASIN count:', len(asins))
    print('Unique ASINs:', list(dict.fromkeys(asins))[:10])
    if label == 'US':
        titles = re.findall(r'<span[^>]*class=["\'][^"\']*a-size-medium[^"\']*?["\'][^>]*>(.*?)</span>', html, re.S)
        print('Found titles:', len(titles))
        print('First 5 titles:')
        for t in titles[:5]:
            clean = re.sub(r'<[^>]+>', '', t).strip()
            print('-', clean)
    else:
        prices = re.findall(r'¥\s*[0-9,]+', html)
        print('JP price matches sample:', prices[:20])
    print()
