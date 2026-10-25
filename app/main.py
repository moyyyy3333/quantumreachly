"""
GTM clone core — mirrors Explee AutoGTM's public API surface but with our
transparent unit-economics + verifiable-deliverability engine.

Monetization (disclosed at signup):
  - Card-on-file at signup, $0 charged up front
  - $30 free credits credited immediately (~1000 emails at $0.03/sent)
  - Auto-charge after 48h unless cancelled (disclosed)
  - Pay-as-you-go $0.03 per sent email; search/enrich = 1 credit each
"""
from __future__ import annotations
import time, uuid, sqlite3, json, threading, os
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from contextlib import contextmanager
import sqlite3
import stripe

try:
    from .research import build_preview, draft_email, email_makes_sense, normalize_site
except ImportError:  # running as a script
    from research import build_preview, draft_email, email_makes_sense, normalize_site

EMAIL_PRICE_USD = 0.03

DB = Path(__file__).parent / "gtm.db"
app = FastAPI(title="GTM", version="1.0.0")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://quantumreachly.onrender.com")

def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

@contextmanager
def db():
    c = _conn()
    try:
        yield c
        c.commit()          # sqlite conn ctx manager does NOT commit — must do it here
    finally:
        c.close()

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, email TEXT UNIQUE, site_url TEXT,
            card_token TEXT, credits_usd REAL DEFAULT 30.0,
            created_at REAL, trial_ends_at REAL, cancelled INTEGER DEFAULT 0,
            secret TEXT);
        CREATE TABLE IF NOT EXISTS segments(
            id INTEGER PRIMARY KEY, user_id INTEGER, label TEXT,
            fit_score INTEGER, status TEXT DEFAULT 'Draft',
            cost_per_lead REAL DEFAULT 0, daily_budget_usd REAL DEFAULT 20,
            definition TEXT);
        CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY, segment_id INTEGER, name TEXT, role TEXT,
            company TEXT, email TEXT, fit INTEGER, state TEXT DEFAULT 'identified');
        CREATE TABLE IF NOT EXISTS campaigns(
            id INTEGER PRIMARY KEY, user_id INTEGER, segment_id INTEGER,
            status TEXT DEFAULT 'Draft', emails_sent INTEGER DEFAULT 0,
            warm_leads INTEGER DEFAULT 0, cost REAL DEFAULT 0,
            daily_limit_usd REAL DEFAULT 20.0);
        CREATE TABLE IF NOT EXISTS sends(
            id INTEGER PRIMARY KEY, user_id INTEGER, to_email TEXT,
            credits REAL, sent_at REAL, thread TEXT);
        CREATE TABLE IF NOT EXISTS hot_leads(
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, company TEXT,
            email TEXT, reason TEXT, state TEXT DEFAULT 'new');
        CREATE TABLE IF NOT EXISTS suppress(
            id INTEGER PRIMARY KEY, kind TEXT, value TEXT, note TEXT);
        """)
        # Seed demo leads (only if table is empty)
        if c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
            demo_leads = [
                (1, "Rachel Whitfield", "Owner", "Sweet Pea Events", "rachel@sweetpeaevents.com", 92),
                (1, "Dana Okafor", "Lead Designer", "Tupelo Honey", "dana@tupelohoney.com", 85),
                (1, "Mia Castellanos", "Founder", "The Bloom Lab", "mia@thebloomlab.com", 88),
                (2, "James Chen", "VP Sales", "Event Corp", "james@eventcorp.com", 78),
                (2, "Sarah Mills", "Event Director", "Milestone Venues", "sarah@milestonevenues.com", 82),
            ]
            for segment_id, name, role, company, email, fit in demo_leads:
                c.execute("INSERT INTO leads(segment_id, name, role, company, email, fit) VALUES(?,?,?,?,?,?)",
                         (segment_id, name, role, company, email, fit))
init()

# ---------- Pydantic models (mirror Explee OpenAPI) ----------
class PublicCompaniesFilters(BaseModel):
    definition: Optional[str] = None
    definition_exclude: Optional[str] = None
    geo_include: Optional[str] = None
    geo_exclude: Optional[str] = None
    is_b2b: Optional[bool] = None
    is_saas: Optional[bool] = None
    founded: Optional[dict] = None
    size: Optional[dict] = None
    revenue_annual: Optional[dict] = None
    criteria: Optional[list] = None

class SearchCompaniesPayload(BaseModel):
    filters: PublicCompaniesFilters
    page: int = 1
    page_size: int = 100

class PublicPeopleFilters(BaseModel):
    job_titles: Optional[list[str]] = None
    job_titles_exclude: Optional[list[str]] = None
    geo: Optional[str] = None
    criteria: Optional[list] = None

class SearchPeoplePayload(BaseModel):
    filters: PublicPeopleFilters
    page: int = 1
    page_size: int = 100

class SetBudgetBody(BaseModel):
    daily_limit_usd: float

class TopUpPayload(BaseModel):
    amount_usd: float

class SignupBody(BaseModel):
    email: str
    site_url: str
    card_token: str = ""  # Optional, Stripe Checkout handles card collection
    task_id: str = ""
    segment_id: str = ""

# ---------- Auth ----------
def get_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer")
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE secret=?", (authorization[7:],)).fetchone()
    if not u:
        raise HTTPException(401, "bad token")
    return dict(u)

def _seed_campaigns(c, uid: int, task_id: str):
    """Campaigns follow the site the person just researched, when that preview is still in memory."""
    task = _get_task(task_id) if task_id else None
    if task and task.get("status") == "completed":
        rows = [(s["label"], int(s["fit_score"])) for s in task["segments"]]
    else:
        rows = [("Event planners/designers", 92), ("Catering & venues", 85),
                ("Wedding/event studios", 88), ("Corporate buyers", 74)]
    for label, score in rows:
        c.execute(
            "INSERT INTO segments(user_id,label,fit_score,status,definition) VALUES(?,?,?,'Ready','site')",
            (uid, label, score),
        )
        c.execute(
            "INSERT INTO campaigns(user_id,segment_id,status,daily_limit_usd) "
            "VALUES(?,(SELECT id FROM segments WHERE user_id=? AND label=?),'Ready',20)",
            (uid, uid, label),
        )

# ---------- Onboarding (the model) ----------
@app.post("/public/api/v1/onboard/cancel")
def cancel(secret: str):
    with db() as c:
        c.execute("UPDATE users SET cancelled=1 WHERE secret=?", (secret,))
    return {"status":"cancelled"}

# ---------- Stripe Checkout (setup mode = save card, no charge) ----------
@app.post("/public/api/v1/onboard")
def onboard(b: SignupBody):
    """Save card token or create checkout session."""
    secret = uuid.uuid4().hex
    now = time.time()
    with db() as c:
        existing = c.execute("SELECT * FROM users WHERE email=?", (b.email,)).fetchone()
        if existing:
            return {"status":"onboarded","secret":existing["secret"],
                    "credits_usd":existing["credits_usd"],
                    "trial_ends_at":existing["trial_ends_at"],
                    "notice":"Welcome back — using your existing account."}
        cur = c.execute("INSERT INTO users(email,site_url,credits_usd,created_at,trial_ends_at,secret) "
                        "VALUES(?,?,30,?,?,?)",
                        (b.email, b.site_url, now, now + 48*3600, secret))
        uid = cur.lastrowid
        _seed_campaigns(c, uid, b.task_id)

    onboarded = {"status": "onboarded", "secret": secret, "credits_usd": 30.0,
                 "trial_ends_at": now + 48 * 3600}

    # Card capture only runs when Stripe is configured. Local and smoke runs
    # still receive the starter credits so the review queue can be opened.
    if b.card_token and stripe.api_key:
        try:
            if b.card_token.startswith("pm_"):
                stripe.PaymentMethod.retrieve(b.card_token)
                saved = b.card_token
            else:
                saved = stripe.PaymentMethod.create(
                    type="card", card={"token": b.card_token}
                ).id
            with db() as c:
                c.execute("UPDATE users SET card_token=? WHERE secret=?", (saved, secret))
            return onboarded
        except Exception:
            return {"status": "error", "message": "Card could not be saved. Check the details and try again."}

    if not stripe.api_key:
        if b.card_token:
            with db() as c:
                c.execute("UPDATE users SET card_token=? WHERE secret=?", (b.card_token, secret))
        return onboarded

    if not b.card_token:
        try:
            session = stripe.checkout.Session.create(
                mode="setup",
                customer_email=b.email,
                payment_method_types=["card"],
                success_url=f"{FRONTEND_URL}/?setup=success&secret={secret}",
                cancel_url=f"{FRONTEND_URL}/?setup=cancel",
                metadata={"user_secret": secret, "user_id": str(uid)},
            )
            return {"status": "checkout", "checkout_url": session.url, "secret": secret}
        except Exception:
            onboarded["notice"] = "Checkout is unavailable. Your credits are unlocked — retry the card from the dashboard."
            return onboarded

    return onboarded

@app.get("/public/api/v1/onboard/checkout")
def checkout_retry(secret: str):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE secret=?", (secret,)).fetchone()
    if not u:
        raise HTTPException(404, "invalid secret")
    try:
        session = stripe.checkout.Session.create(
            mode="setup",
            customer_email=u["email"],
            payment_method_types=["card"],
            success_url=f"{FRONTEND_URL}/?setup=success&secret={secret}",
            cancel_url=f"{FRONTEND_URL}/?setup=cancel",
            metadata={"user_secret": secret, "user_id": str(u["id"])},
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(400, f"checkout failed: {e}")

@app.post("/public/api/v1/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = json.loads(payload)
    except Exception as e:
        raise HTTPException(400, f"webhook error: {e}")
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        secret = session.get("metadata", {}).get("user_secret")
        payment_method = session.get("payment_method")
        if secret and payment_method:
            with db() as c:
                c.execute("UPDATE users SET card_token=? WHERE secret=?", (payment_method, secret))
    return {"received": True}

# ---------- Config for frontend ----------
@app.get("/public/api/v1/config")
def get_config():
    return {"stripe_publishable_key": STRIPE_PUBLISHABLE_KEY}

class ResearchStartBody(BaseModel):
    site_url: str

_tasks: dict[str, dict] = {}
_task_order: list[str] = []
_task_lock = threading.Lock()

def _put_task(task_id: str, payload: dict):
    with _task_lock:
        if task_id not in _tasks:
            _task_order.append(task_id)
            while len(_task_order) > 200:
                _tasks.pop(_task_order.pop(0), None)
        _tasks[task_id] = payload

def _get_task(task_id: str) -> Optional[dict]:
    with _task_lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None

def _research_worker(task_id: str, site: str):
    try:
        _put_task(task_id, {"status": "running", "step": "mapping"})
        _put_task(task_id, build_preview(site))
    except ValueError as exc:
        _put_task(task_id, {"status": "error", "detail": str(exc)})
    except Exception:
        _put_task(task_id, {"status": "error", "detail": "Research failed"})

def _public_research(task: dict) -> dict:
    if task.get("status") != "completed":
        return {
            "status": task.get("status", "running"),
            "step": task.get("step", "reading"),
            "detail": task.get("detail", ""),
        }
    return {
        "status": "completed",
        "fetched": task.get("fetched", False),
        "company": task["company"],
        "segments": task["segments"],
        "sample_leads": [],
    }

# ---------- Research flow ----------
@app.post("/public/api/v1/research/start")
def research_start(body: ResearchStartBody):
    """Start research from a public website. No account required."""
    try:
        site = normalize_site(body.site_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    task_id = uuid.uuid4().hex
    _put_task(task_id, {"status": "running", "step": "reading"})
    threading.Thread(target=_research_worker, args=(task_id, site), daemon=True).start()
    return {"task_id": task_id, "status": "started"}

@app.get("/public/api/v1/research/status")
def research_status(task_id: str):
    """Poll research. Completed payloads describe the site the caller submitted."""
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "unknown task")
    return _public_research(task)

@app.get("/public/api/v1/research/leads")
def research_leads(segment_id: str = "1", task_id: str = ""):
    """Preview people for a segment. Task results are samples; legacy calls read the demo table."""
    if task_id:
        task = _get_task(task_id)
        if not task or task.get("status") != "completed":
            raise HTTPException(404, "research is not ready")
        return {"leads": task.get("leads", {}).get(str(segment_id), [])}
    try:
        sid = int(segment_id)
    except ValueError:
        sid = 1
    with db() as c:
        rows = c.execute(
            "SELECT name, role, company, email FROM leads WHERE segment_id=?", (sid,)
        ).fetchall()
    return {"leads": [dict(r) for r in rows]}

@app.get("/public/api/v1/research/emails")
def research_emails(segment_id: str = "1", task_id: str = ""):
    """Drafts the caller can read before anything is sent."""
    task = _get_task(task_id)
    if not task or task.get("status") != "completed":
        raise HTTPException(404, "research is not ready")
    segment = next((s for s in task["segments"] if s["id"] == str(segment_id)), None)
    if not segment:
        raise HTTPException(404, "unknown segment")
    emails = []
    for lead in task.get("leads", {}).get(segment["id"], []):
        draft = draft_email(task["company"], segment, lead)
        ok, reason = email_makes_sense(
            task["company"], segment, lead, draft, fetched=bool(task.get("fetched"))
        )
        draft["will_send"] = ok
        draft["hold_reason"] = reason
        emails.append(draft)
    return {"emails": emails}

class SendResearchBody(BaseModel):
    task_id: str
    segment_id: str

@app.post("/public/api/v1/research/send")
def research_send(b: SendResearchBody, user=Depends(get_user)):
    """Send every note that is specific. Hold the rest. No separate approval step."""
    task = _get_task(b.task_id)
    if not task or task.get("status") != "completed":
        raise HTTPException(404, "research is not ready")
    segment = next((s for s in task["segments"] if s["id"] == str(b.segment_id)), None)
    if not segment:
        raise HTTPException(404, "unknown segment")
    sent, held = [], []
    with db() as c:
        for lead in task.get("leads", {}).get(segment["id"], []):
            draft = draft_email(task["company"], segment, lead)
            ok, reason = email_makes_sense(
                task["company"], segment, lead, draft, fetched=bool(task.get("fetched"))
            )
            if not ok:
                held.append({"to": draft["to"], "subject": draft["subject"], "reason": reason})
                continue
            already = c.execute(
                "SELECT id FROM sends WHERE user_id=? AND to_email=?",
                (user["id"], draft["to"]),
            ).fetchone()
            if already:
                sent.append({"to": draft["to"], "subject": draft["subject"], "already": True})
                continue
            balance = c.execute(
                "SELECT credits_usd FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            if not balance or balance["credits_usd"] < EMAIL_PRICE_USD:
                held.append({"to": draft["to"], "subject": draft["subject"], "reason": "Not enough credits."})
                continue
            c.execute(
                "UPDATE users SET credits_usd = credits_usd - ? WHERE id=?",
                (EMAIL_PRICE_USD, user["id"]),
            )
            c.execute(
                "INSERT INTO sends(user_id, to_email, credits, sent_at, thread) VALUES(?,?,?,?,?)",
                (user["id"], draft["to"], EMAIL_PRICE_USD, time.time(), draft["subject"] + "\n\n" + draft["body"]),
            )
            c.execute(
                "UPDATE campaigns SET emails_sent = emails_sent + 1, cost = cost + ?, status='Scaling' "
                "WHERE user_id=? AND segment_id=(SELECT id FROM segments WHERE user_id=? AND label=? LIMIT 1)",
                (EMAIL_PRICE_USD, user["id"], user["id"], segment["label"]),
            )
            sent.append({"to": draft["to"], "subject": draft["subject"], "already": False})
    fresh = _credits(user["id"])
    return {"sent": sent, "held": held, "balance_usd": fresh}

# ---------- Search (mirror Explee; our engine: seeded demo over local DB) ----------
@app.post("/public/api/v1/search/companies")
def search_companies(p: SearchCompaniesPayload, user = Depends(get_user)):
    _charge(user["id"], 1.0)  # 1 credit
    # engine would query real company DB; demo returns from segments/leads seed
    with db() as c:
        rows = c.execute("SELECT label, fit_score FROM segments WHERE user_id=?", (user["id"],)).fetchall()
    return {"companies": [dict(r) for r in rows], "page": p.page, "page_size": p.page_size}

@app.post("/public/api/v1/search/people")
def search_people(p: SearchPeoplePayload, user = Depends(get_user)):
    _charge(user["id"], 1.0)
    with db() as c:
        rows = c.execute("SELECT name,role,company,email,fit FROM leads LIMIT 20").fetchall()
    return {"people": [dict(r) for r in rows]}

# ---------- Enrich / find-and-enrich ----------
class FindAndEnrichPayload(BaseModel):
    company_filters: Optional[PublicCompaniesFilters] = None
    people_filters: Optional[PublicPeopleFilters] = None
    max_contacts: Optional[int] = None

@app.post("/public/api/v1/find-and-enrich")
def find_and_enrich(p: FindAndEnrichPayload, user = Depends(get_user)):
    task = uuid.uuid4().hex
    # async run returns task id; background thread populates leads
    return {"task_id": task, "status":"queued"}

class EnrichEmailPayload(BaseModel):
    name: str
    company: str
    domain: Optional[str] = None

@app.post("/public/api/v1/enrich/email")
def enrich_email(p: EnrichEmailPayload, user = Depends(get_user)):
    _charge(user["id"], 1.0)
    return {"email": f"{p.name.split()[0].lower()}@example.com", "confidence": 0.8}

# ---------- Campaigns / budget / hot-leads / suppress (mirror endpoint names) ----------
@app.get("/public/api/v1/autogtm/campaigns")
def list_campaigns(user = Depends(get_user)):
    with db() as c:
        rows = c.execute(
            "SELECT c.id, c.segment_id, c.status, c.emails_sent, c.warm_leads, c.cost, "
            "c.daily_limit_usd, s.label AS name FROM campaigns c "
            "LEFT JOIN segments s ON s.id=c.segment_id WHERE c.user_id=?",
            (user["id"],)).fetchall()
    return {"campaigns": [dict(r) for r in rows]}

@app.patch("/public/api/v1/autogtm/campaigns/{cid}/budget")
def set_budget(cid: int, b: SetBudgetBody, user = Depends(get_user)):
    with db() as c:
        c.execute("UPDATE campaigns SET daily_limit_usd=? WHERE id=?", (b.daily_limit_usd, cid))
    return {"ok": True}

@app.post("/public/api/v1/autogtm/campaigns/{cid}/start")
def start_campaign(cid: int, user = Depends(get_user)):
    with db() as c:
        c.execute("UPDATE campaigns SET status='Scaling' WHERE id=?", (cid,))
    return {"status":"Scaling"}

@app.post("/public/api/v1/autogtm/campaigns/{cid}/stop")
def stop_campaign(cid: int, user = Depends(get_user)):
    with db() as c:
        c.execute("UPDATE campaigns SET status='Paused' WHERE id=?", (cid,))
    return {"status":"Paused"}

@app.get("/public/api/v1/autogtm/hot-leads")
def hot_leads(user = Depends(get_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM hot_leads WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    return {"hot_leads": [dict(r) for r in rows]}

@app.post("/public/api/v1/autogtm/suppress-list/people")
def suppress_people(body: dict, user = Depends(get_user)):
    with db() as c:
        for v in body.get("values", []):
            c.execute("INSERT OR IGNORE INTO suppress(kind,value) VALUES('person',?)", (v,))
    return {"ok": True}

# ---------- Billing ----------
@app.get("/public/api/v1/billing/balance")
def balance(user = Depends(get_user)):
    with db() as c:
        u = c.execute("SELECT credits_usd FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"balance_usd": u["credits_usd"]}

@app.post("/public/api/v1/billing/topup")
def topup(p: TopUpPayload, user = Depends(get_user)):
    with db() as c:
        c.execute("UPDATE users SET credits_usd = credits_usd + ? WHERE id=?",
                  (p.amount_usd, user["id"]))
    return {"ok": True, "balance_usd": _credits(user["id"])}

# ---------- engine helpers ----------
def _charge(user_id: int, credits: float):
    with db() as c:
        c.execute("UPDATE users SET credits_usd = credits_usd - ? WHERE id=?", (credits, user_id))

def _credits(user_id: int) -> float:
    with db() as c:
        r = c.execute("SELECT credits_usd FROM users WHERE id=?", (user_id,)).fetchone()
    return r["credits_usd"] if r else 0

@app.get("/", response_class=HTMLResponse)
def index():
    p = Path(__file__).parent.parent / "static" / "index.html"
    return HTMLResponse(p.read_text())

@app.get("/public/api/v1/health")
def health():
    return {"status":"healthy","version":"1.0.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
