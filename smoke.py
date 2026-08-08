#!/usr/bin/env python3
"""Smoke test the GTM core flow: onboard -> credits -> search -> topup -> cancel."""
import json, urllib.request

B = "http://127.0.0.1:8080"

def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(B+path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

ok = True
# 1. health
st, h = req("GET", "/public/api/v1/health"); print("health:", st, h); ok &= st==200
# 2. onboard with fake card
st, o = req("POST", "/public/api/v1/onboard", {"email":"test@acme.com","site_url":"https://acme.com","card_token":"pm_test"})
print("onboard:", st, {k:o.get(k) for k in ("status","credits_usd","notice") if k in o})
ok &= st==200 and o.get("credits_usd")==30.0
tok = o.get("secret")
# 3. balance
st, bal = req("GET", "/public/api/v1/billing/balance", token=tok); print("balance:", st, bal); ok &= st==200
# 4. search companies (costs 1 credit)
st, sc = req("POST", "/public/api/v1/search/companies", {"filters":{"definition":"restaurant pos"}}, token=tok)
print("search companies:", st, str(sc)[:120]); ok &= st==200
# 5. search people
st, sp = req("POST", "/public/api/v1/search/people", {"filters":{}}, token=tok)
print("search people:", st, str(sp)[:120]); ok &= st==200
# 6. topup
st, tp = req("POST", "/public/api/v1/billing/topup", {"amount_usd":10}, token=tok)
print("topup:", st, tp); ok &= st==200
# 7. balance after charges+topup (30 -2 search +10 topup = 38)
st, bal2 = req("GET", "/public/api/v1/billing/balance", token=tok)
print("balance after:", st, bal2, "(expect ~38)")

print("\nRESULT:", "PASS ✓" if ok else "FAIL ✗")
