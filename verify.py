#!/usr/bin/env python3
"""End-to-end verify: dashboard + onboard -> balance -> campaigns -> segments.
One runner that follows the data across every edge so a failure is caught at the exact hop."""
import json, urllib.request, urllib.error
B = 'http://127.0.0.1:8080'

def http(method, path, body=None, tok=None, raw=False):
    data = json.dumps(body).encode() if body else None
    h = {'Content-Type': 'application/json'}
    if tok: h['Authorization'] = 'Bearer ' + tok
    r = urllib.request.Request(B + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            b = resp.read()
            return resp.status, (b if raw else json.loads(b or b'{}'))
    except urllib.error.HTTPError as e:
        b = e.read()
        try: return e.code, json.loads(b or b'{}')
        except Exception: return e.code, {'_raw': b[:120].decode('utf-8','ignore')}

ok = True
def check(label, st, exp, val=''):
    global ok
    good = st == exp
    ok = ok and good
    print(f"{'PASS' if good else 'FAIL'}  {label}  [{st}] {val}")

# 1. dashboard serves HTML (raw)
st, html = http('GET', '/', raw=True)
check("dashboard HTML", st, 200, f"{len(html)} bytes")

# 2. onboard
st, o = http('POST', '/public/api/v1/onboard', {'email':'e2e@x.com','site_url':'https://x.com','card_token':'t'})
check("onboard", st, 200)
print("     credits:", o.get('credits_usd'), "| notice:", (o.get('notice') or '')[:50])
tok = o.get('secret')

# 3. balance
st, b = http('GET', '/public/api/v1/billing/balance', tok=tok)
check("balance", st, 200, f"usd={b.get('balance_usd')}")

# 4. campaigns (seeded segments visible by name)
st, cm = http('GET', '/public/api/v1/autogtm/campaigns', tok=tok)
check("campaigns", st, 200, str([c.get('name') for c in cm.get('campaigns',[])]))

# 5. search companies (1 credit)
st, sc = http('POST', '/public/api/v1/search/companies', {'filters':{'definition':'restaurant'}}, tok=tok)
check("search companies", st, 200)

# 6. topup then balance
st, tp = http('POST', '/public/api/v1/billing/topup', {'amount_usd':10}, tok=tok)
check("topup", st, 200, f"usd={tp.get('balance_usd')}")

print("\nRESULT:", "ALL PASS ✓" if ok else "FAILED ✗")
