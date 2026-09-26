#!/usr/bin/env python3
"""End-to-end verify: dashboard + onboard -> balance -> campaigns -> segments.
One runner that follows the data across every edge so a failure is caught at the exact hop."""
import json, time, urllib.request, urllib.error
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

# 7. research follows the submitted site, and refuses local targets
st, blocked = http('POST', '/public/api/v1/research/start', {'site_url':'http://127.0.0.1/admin'})
check("block local research", st, 400, str(blocked.get('detail',''))[:60])

st, started = http('POST', '/public/api/v1/research/start', {'site_url':'example.com'})
check("research start", st, 200, started.get('task_id',''))
task = started.get('task_id')
preview = {}
for _ in range(40):
    st, preview = http('GET', '/public/api/v1/research/status?task_id=' + task)
    if preview.get('status') in ('completed', 'error'):
        break
    time.sleep(0.5)
check("research status", st, 200, preview.get('status',''))
domain = (preview.get('company') or {}).get('domain','')
ok = ok and domain == 'example.com' and preview.get('status') == 'completed'
print(f"{'PASS' if domain=='example.com' and preview.get('status')=='completed' else 'FAIL'}  research domain  [{domain}]")
segs = preview.get('segments') or []
seg_id = segs[0]['id'] if segs else '1'
st, leads = http('GET', f'/public/api/v1/research/leads?task_id={task}&segment_id={seg_id}')
sample = (leads.get('leads') or [{}])[0].get('email','')
check("preview leads", st, 200, sample)
ok = ok and sample.endswith('.example')
print(f"{'PASS' if sample.endswith('.example') else 'FAIL'}  sample inbox stays on .example")
st, emails = http('GET', f'/public/api/v1/research/emails?task_id={task}&segment_id={seg_id}')
body = ((emails.get('emails') or [{}])[0].get('body') or '')
check("preview emails", st, 200, f"{len(body)} chars")
ok = ok and 'example.com' in body
will_send = all(m.get('will_send') for m in emails.get('emails') or [])
print(f"{'PASS' if will_send else 'FAIL'}  sensible notes are marked to send")
ok = ok and will_send

st, acct = http('POST', '/public/api/v1/onboard', {
    'email': f'send{int(time.time())}@example.com',
    'site_url': 'https://example.com',
    'card_token': 't',
    'task_id': task,
    'segment_id': seg_id,
})
check("onboard for send", st, 200)
st, sent = http('POST', '/public/api/v1/research/send', {'task_id': task, 'segment_id': seg_id}, tok=acct.get('secret'))
n_sent = len(sent.get('sent') or [])
n_held = len(sent.get('held') or [])
check("auto send", st, 200, f"sent={n_sent} held={n_held} usd={sent.get('balance_usd')}")
ok = ok and n_sent >= 1 and n_held == 0 and abs((sent.get('balance_usd') or 0) - (30 - 0.03 * n_sent)) < 0.001

print("\nRESULT:", "ALL PASS ✓" if ok else "FAILED ✗")
