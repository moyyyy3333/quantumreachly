"""Turn a public website URL into a reviewable buyer preview.

The preview is built from the homepage (title and description) when the page
is reachable, and from the domain when it is not. Sample people use the
reserved .example domain so a draft can never point at a real inbox.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qr-dns")

import httpx

_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_MAX_HTML = 200_000
_UA = "QuantumReachlyBot/1.0 (+https://quantumreachly.onrender.com)"

# (keywords, segments). First match wins. Fit scores are a ranking hint for
# the preview, not a measured conversion rate.
_SEGMENT_RULES: list[tuple[tuple[str, ...], list[tuple[str, int, str]]]] = [
    (
        ("event", "wedding", "venue", "florist", "catering", "planner"),
        [
            ("Event planners and designers", 92, "They pick vendors for a client and can tell a bad fit in one read."),
            ("Venue and catering leads", 86, "They buy anything that fills dates and cuts the back-and-forth."),
            ("Studio owners", 84, "Small teams where the founder still answers the inbox."),
            ("In-house event buyers", 76, "Coordinators with a budget cycle and a calendar to defend."),
        ],
    ),
    (
        ("recruit", "hiring", "talent", "job", "staffing", "hr"),
        [
            ("Heads of talent", 91, "They own the req list and feel a slow funnel immediately."),
            ("Founders who are still hiring", 88, "They do the first screen themselves and have no recruiter yet."),
            ("Staffing agency owners", 83, "They live on fill-rate and will try a channel that saves a search."),
            ("People operations leads", 77, "They run the process after the hiring manager says yes."),
        ],
    ),
    (
        ("restaurant", "food", "cafe", "menu", "kitchen", "hospitality"),
        [
            ("Independent restaurant owners", 90, "They buy tools that show up in the same week, not next quarter."),
            ("Multi-unit operators", 85, "They care about the same problem repeating across locations."),
            ("Catering and private-dining leads", 82, "They sell high-ticket events and answer their own email."),
            ("Hospitality group marketers", 74, "They need a reason to open a vendor conversation."),
        ],
    ),
    (
        ("real estate", "property", "realtor", "broker", "mortgage"),
        [
            ("Independent agents", 90, "They pay for pipeline out of their own commission."),
            ("Team leads at brokerages", 86, "They decide what the desk is allowed to use."),
            ("Property managers", 81, "They buy when it saves a recurring operational hour."),
            ("Mortgage and title partners", 75, "They want referral flow, not another portal login."),
        ],
    ),
    (
        ("clinic", "dental", "patient", "health", "therapy", "wellness"),
        [
            ("Practice owners", 91, "They approve spend and still see the front desk."),
            ("Office managers", 86, "They feel no-shows and empty chairs first."),
            ("Multi-location operators", 82, "They want one motion that works at every site."),
            ("Specialty clinic directors", 76, "They buy when the note is specific to their line of care."),
        ],
    ),
    (
        ("shop", "store", "ecommerce", "retail", "sku", "checkout"),
        [
            ("Store founders", 90, "They still read customer email and notice a wasted ad dollar."),
            ("Heads of ecommerce", 86, "They own the channel mix and a monthly target."),
            ("Retail buyers", 80, "They take meetings that reference their assortment."),
            ("Agency operators for DTC brands", 74, "They need a story they can retell to a client."),
        ],
    ),
    (
        ("software", "saas", "developer", "api", "platform", "app"),
        [
            ("Founders at seed to series B", 92, "They still approve the first outbound themselves."),
            ("Heads of growth", 87, "They need pipeline that does not wait on a new hire."),
            ("Agency owners serving this category", 82, "They resell a motion once it works on one account."),
            ("Revenue leaders", 76, "They will look if the draft sounds like their market."),
        ],
    ),
]

_DEFAULT_SEGMENTS = [
    ("Founders who would buy this", 90, "They still approve the first vendor conversation themselves."),
    ("Operators who feel the problem", 85, "They notice the manual work this replaces."),
    ("Agency owners in this market", 80, "They will reuse a motion that works on one account."),
    ("Budget holders", 74, "They need a specific reason, not a generic intro."),
]


def clean_text(value: str, limit: int = 280) -> str:
    text = unescape(re.sub(r"(?is)<script[^>]*>.*?</script>", " ", value or ""))
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def host_is_blocked(host: str) -> bool:
    """True for loopback, private, link-local, and local-only names."""
    name = (host or "").strip().lower().rstrip(".")
    if not name or name in _LOCAL_HOSTS or name.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return not ip.is_global


def _resolves_public(host: str) -> bool:
    if host_is_blocked(host):
        return False
    try:
        infos = _dns_pool.submit(socket.getaddrinfo, host, None).result(timeout=3)
    except (FuturesTimeout, socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def normalize_site(raw: str) -> str:
    """Return an http(s) URL or raise ValueError. Rejects local targets."""
    text = (raw or "").strip()
    if not text or len(text) > 300:
        raise ValueError("Add a website first")
    if not re.match(r"https?://", text, re.I):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("That website address is not usable")
    if parsed.username or parsed.password or host_is_blocked(host):
        raise ValueError("That website address is not reachable")
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path or ""
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{name}\s*=\s*("|\')(.*?)\1', tag, re.I | re.S)
    return clean_text(match.group(2), 300) if match else ""


def _meta_content(html: str, *, prop: Optional[str] = None, name: Optional[str] = None) -> str:
    for tag in re.findall(r"(?is)<meta\b[^>]*>", html):
        key = _attr(tag, "property") or _attr(tag, "name")
        if prop and key.lower() == prop:
            return _attr(tag, "content")
        if name and key.lower() == name:
            return _attr(tag, "content")
    return ""


def parse_homepage(html: str) -> tuple[str, str]:
    title = ""
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    if match:
        title = clean_text(match.group(1), 80)
    site_name = _meta_content(html, prop="og:site_name")
    og_title = _meta_content(html, prop="og:title")
    description = _meta_content(html, prop="og:description") or _meta_content(html, name="description")
    name = site_name or og_title or title
    # Drop a tagline glued on with a separator: "Acme | The fastest way..."
    name = re.split(r"\s+[|\-–—:]\s+", name, maxsplit=1)[0].strip()
    return clean_text(name, 60), clean_text(description, 220)


def fetch_public_html(url: str) -> Optional[str]:
    """GET a public page. Returns None if the host is blocked or unreachable."""
    current = url
    try:
        with httpx.Client(timeout=4.0, follow_redirects=False, headers={"User-Agent": _UA}) as client:
            for _ in range(3):
                parsed = urlparse(current)
                host = (parsed.hostname or "").lower()
                if parsed.scheme not in ("http", "https") or not host or not _resolves_public(host):
                    return None
                response = client.get(current)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    return None
                ctype = response.headers.get("content-type", "")
                if ctype and "html" not in ctype and "text/" not in ctype:
                    return None
                return response.text[:_MAX_HTML]
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return None


def brand_from_host(host: str) -> str:
    label = host.lower()
    if label.startswith("www."):
        label = label[4:]
    stem = label.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", stem) if part) or host


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def first_paragraphs(html: str) -> str:
    parts: list[str] = []
    for match in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", html or ""):
        inner = re.sub(
            r"(?is)<a\b[^>]*>\s*(learn more|read more|click here|more information)\s*</a>",
            " ",
            match.group(1),
        )
        bit = clean_text(inner, 220)
        if not bit or bit.lower() in {"learn more", "read more", "click here", "more information"}:
            continue
        parts.append(bit)
        if len(" ".join(parts)) > 180:
            break
    return clean_text(" ".join(parts), 220)


def pick_segments(blob: str) -> list[dict]:
    text = blob.lower()
    chosen = _DEFAULT_SEGMENTS
    for words, segments in _SEGMENT_RULES:
        if any(_has_word(text, word) for word in words):
            chosen = segments
            break
    return [
        {"id": str(index), "label": label, "fit_score": score, "why": why}
        for index, (label, score, why) in enumerate(chosen, start=1)
    ]


_FIRST = ("Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn")
_LAST = ("Nguyen", "Patel", "Brooks", "Okoye", "Silva", "Hart")
_ROLES = ("Founder", "Head of Operations", "Director")
_FIRMS = ("Northwind Studio", "Brightline Supply", "Harbor and Co")


def sample_leads(segment: dict) -> list[dict]:
    """Three fictional preview contacts. Emails stay on .example."""
    slot = int(segment["id"])
    leads = []
    for offset in range(3):
        idx = (slot + offset) % len(_FIRST)
        firm = _FIRMS[offset]
        slug = re.sub(r"[^a-z0-9]+", "", firm.lower())[:18] or "sample"
        first, last = _FIRST[idx], _LAST[(idx + slot) % len(_LAST)]
        leads.append(
            {
                "name": f"{first} {last}",
                "role": _ROLES[offset],
                "company": firm,
                "email": f"{first}.{last}@{slug}.example".lower(),
                "fit": max(70, segment["fit_score"] - offset * 4),
            }
        )
    return leads


def draft_email(company: dict, segment: dict, lead: dict) -> dict:
    first = lead["name"].split()[0]
    subject = f"{first}, a note from {company['name']}"
    body = (
        f"Hi {first},\n\n"
        f"{company['description']}\n\n"
        f"I am writing from {company['name']} ({company['domain']}) because {lead['company']} "
        f"is the kind of account we had in mind for {segment['label'].lower()}. "
        f"You are the {lead['role'].lower()}, so I wanted you to see it first.\n\n"
        f"{segment['why']}\n\n"
        f"If this is relevant I can send a shorter version. If it is not, I will close the loop.\n\n"
        f"— {company['name']}"
    )
    return {"to": lead["email"], "name": lead["name"], "subject": subject, "body": body}


def build_preview(site_url: str) -> dict:
    """Research one public site into a company, segments, and preview leads."""
    normalized = normalize_site(site_url)
    html = fetch_public_html(normalized)
    title, description = parse_homepage(html or "")
    if html and not description:
        description = first_paragraphs(html)
    host = urlparse(normalized).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    brand = brand_from_host(host)
    name = title or brand
    if not description:
        description = f"{name} is the company behind {host}."
    blob = " ".join([host, title, description])
    segments = pick_segments(blob)
    leads = {segment["id"]: sample_leads(segment) for segment in segments}
    return {
        "status": "completed",
        "fetched": bool(html),
        "site_url": normalized,
        "company": {"name": name, "domain": host, "description": description},
        "segments": segments,
        "leads": leads,
    }
