import urllib.request
import urllib.parse

url = 'https://api.keepa.com/search'
params = {
    'key': 'p48cqmee14813fmfl477t3bne2vfq1f360ra3qj2e11m445g4p7bbk2lc9mr9oh8',
    'domain': 1,
    'type': 'product',
    'term': 'tamashii nations',
    'buyBoxMin': 3000,
    'availabilityAmazon': -1,
    'offers': 1,
    'history': 0,
}
request_url = url + '?' + urllib.parse.urlencode(params)
print('request_url', request_url)
print('getproxies', urllib.request.getproxies())
for name, opener in [('default', urllib.request.build_opener()), ('noproxy', urllib.request.build_opener(urllib.request.ProxyHandler({})))]:
    print('---', name)
    try:
        with opener.open(request_url, timeout=10) as r:
            print('status', r.status)
            print('headers', dict(r.headers.items()))
            print('body', r.read(100))
    except Exception as e:
        import traceback
        traceback.print_exc()
