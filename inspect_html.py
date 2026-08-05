import gzip
import re
from pathlib import Path

for path_str in ['/tmp/amazon_us_pilot.html', '/tmp/amazon_jp_pilot.html']:
    path = Path(path_str)
    print('===', path_str, '===')
    raw = path.read_bytes()
    if raw.startswith(b'\x1f\x8b\x08'):
        html = gzip.decompress(raw).decode('utf-8', errors='ignore')
        print('compressed gzip content, decompressed len', len(html))
    else:
        html = raw.decode('utf-8', errors='ignore')
        print('plain text len', len(html))
    print('contains body', '<body' in html)
    print('contains amazon', 'amazon' in html.lower())
    for pat in ['data-asin=', 'href="/dp/', 'a-size-medium', 'Pilot', 'Frixion', 'Amazon.com']:
        print(pat, html.count(pat))
    if path_str.endswith('pilot.html') and 'Pilot' in html:
        idx = html.find('Pilot')
        sample = html[max(0, idx-200):idx+200]
        print('--- sample around Pilot ---')
        print(sample)
    print('first 5 /dp/ asins:', re.findall(r'/dp/([A-Z0-9]{10})', html)[:5])
    print('---')
