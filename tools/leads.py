"""Lead enrichment, research, CSV save/load, and outreach CSV."""
import concurrent.futures
import csv
import os
import re
from datetime import datetime

from core.state import get_leads, set_leads, get_outreach, set_outreach

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_BASE_DIR, "..")

LEAD_FIELDS = [
    "trade_name", "entity_name", "formation_date", "years_in_business",
    "sunbiz_status", "sunbiz_url", "general_email", "business_phone",
    "address", "website", "owner_name", "owner_email", "owner_phone",
    "registered_agent", "reg_agent_address", "reg_agent_email", "reg_agent_phone",
    "instagram_url", "facebook_url", "google_review_count", "google_rating",
    "industry", "employees", "linkedin_url",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_leads_to_file(leads: list) -> None:
    try:
        path = os.path.join(_REPO_ROOT, "leads.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LEAD_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(leads)
    except Exception:
        pass


def _find_person_contact(name, business_name="", city="", state="") -> dict:
    """Web-search for a person's email and phone number."""
    from tools.search import web_search

    if not name:
        return {"email": "", "phone": ""}
    noise = {
        "example", "sentry", "wixpress", "squarespace", "domain",
        "noreply", "wordpress", "schema", "w3.org", "google", "yelp", "facebook",
    }

    def _extract(text):
        emails = re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', text)
        email  = ""
        for e in emails:
            el = e.lower()
            if not any(n in el for n in noise) and len(el) < 60:
                email = el
                break
        phones = re.findall(r'\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}', text)
        phone  = phones[0].strip() if phones else ""
        return email, phone

    queries = []
    if business_name:
        queries.append(f'"{name}" "{business_name}" email contact')
        queries.append(f'"{name}" {city} {state} {business_name} owner email')
    queries.append(f'"{name}" {city} {state} email phone')

    for query in queries:
        try:
            result = web_search(query.strip())
            text   = result.get("results", "")
            email, phone = _extract(text)
            if email or phone:
                return {"email": email, "phone": phone}
        except Exception:
            continue

    return {"email": "", "phone": ""}


def _compute_expansion_probability(data: dict) -> dict:
    """Score 0-99 + signal list based on available research data."""
    score   = 35
    signals = []
    sb      = data.get("sunbiz", {})
    cd      = data.get("contact_data", {})

    status = (sb.get("status") or sb.get("sunbiz_status") or "").lower()
    if "active" in status:
        score += 10

    rating, reviews = 0.0, 0
    try:  rating  = float(data.get("google_rating") or 0)
    except Exception: pass
    try:  reviews = int(str(data.get("google_reviews") or 0).replace(",", ""))
    except Exception: pass

    if rating >= 4.5:
        score += 20
        signals.append(f"High customer satisfaction ({rating}★ Google rating across {reviews:,} reviews)")
    elif rating >= 4.0:
        score += 10
        if reviews:
            signals.append(f"Strong online reputation ({rating}★ across {reviews:,} reviews)")

    if reviews >= 300:
        score += 5

    years = 0
    try:
        years = int(str(sb.get("years_in_business") or "").split()[0])
    except Exception:
        try:
            fd = sb.get("formation_date", "")
            if fd:
                d     = datetime.strptime(fd, "%m/%d/%Y")
                years = int((datetime.now() - d).days / 365)
        except Exception:
            pass

    if years >= 5:
        score += 15
        signals.append(f"{years}-year operational history demonstrates business stability and expansion readiness")
    elif years >= 3:
        score += 8
        signals.append(f"{years} years in business — approaching prime expansion window")

    if data.get("website"):
        score += 5
    if cd.get("instagram_url"):
        score += 8
        signals.append("Active social media presence indicates marketing momentum and brand awareness")
    if cd.get("facebook_url"):
        score += 3

    owner = sb.get("owner_name") or ""
    if owner:
        score += 5
        signals.append(f"Owner {owner.title()} identified — direct decision-maker outreach possible")

    owner_email = sb.get("owner_email") or cd.get("owner_email") or ""
    owner_phone = sb.get("owner_phone") or cd.get("owner_phone") or ""
    if owner_email or owner_phone:
        score += 5
        signals.append("Direct owner contact info verified — high response probability for outreach")

    if len(signals) < 3:
        signals.append("Single-location operation — minimal competitive risk from multi-location chains")
    if len(signals) < 3 and years >= 3:
        signals.append("Consistent operations over multiple years signal financial health and growth potential")

    return {"score": min(96, max(25, score)), "signals": signals[:6]}


# ── Public tools ──────────────────────────────────────────────────────────────

def get_collected_leads() -> dict:
    leads = get_leads()
    if not leads:
        return {"leads": [], "count": 0, "message": "No leads collected yet in this session."}
    return {"leads": leads, "count": len(leads)}


def save_leads_csv(leads: list) -> dict:
    set_leads(leads)
    _save_leads_to_file(leads)
    return {"success": True, "count": len(leads), "path": "leads.csv"}


def save_outreach_csv(drafts: list) -> dict:
    set_outreach(drafts)
    try:
        path = os.path.join(_REPO_ROOT, "outreach_drafts.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["name", "email", "subject_line", "email_body"],
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(drafts)
        return {"success": True, "path": path, "count": len(drafts), "drafts": drafts}
    except Exception as e:
        return {"error": str(e)}


def enrich_leads_batch(leads=None) -> dict:
    """Enrich every lead in parallel with Sunbiz, website, Google reviews, and web-search contacts."""
    from tools.browser import sunbiz_lookup, scrape_website_contact, get_google_reviews

    if not leads:
        leads = list(get_leads())
    if not leads:
        return {"error": "No leads to enrich. Run search_businesses_maps first.", "leads": []}

    def _enrich_one(lead):
        result = dict(lead)
        for f in LEAD_FIELDS:
            result.setdefault(f, "")

        name  = result.get("trade_name", "")
        url   = result.get("website", "")
        city  = result.get("city", "")
        state = result.get("state", "")

        try:
            sb = sunbiz_lookup(name)
            if sb.get("found"):
                result["entity_name"]       = sb.get("entity_name", "")
                result["formation_date"]    = sb.get("date_filed", "")
                result["years_in_business"] = sb.get("years_in_business", "")
                result["sunbiz_status"]     = sb.get("sunbiz_status", "")
                result["sunbiz_url"]        = sb.get("sunbiz_url", "")
                result["registered_agent"]  = sb.get("registered_agent", "")
                result["reg_agent_address"] = sb.get("reg_agent_address", "")
                if sb.get("owner_name") and not result.get("owner_name"):
                    result["owner_name"] = sb.get("owner_name", "")
        except Exception:
            pass

        if url:
            try:
                ws = scrape_website_contact(url)
                if ws.get("general_email"):
                    result["general_email"] = ws["general_email"]
                if ws.get("instagram_url"):
                    result["instagram_url"] = ws["instagram_url"]
                if ws.get("facebook_url") and not result.get("facebook_url"):
                    result["facebook_url"] = ws["facebook_url"]
                if not result.get("business_phone") and ws.get("phones"):
                    result["business_phone"] = ws["phones"][0]
            except Exception:
                pass

        if not (result.get("google_rating") and result.get("google_review_count")):
            try:
                gr = get_google_reviews(name, city, state)
                if gr.get("google_rating"):
                    result["google_rating"] = gr["google_rating"]
                if gr.get("google_review_count"):
                    result["google_review_count"] = gr["google_review_count"]
            except Exception:
                pass

        owner = result.get("owner_name", "")
        if owner and not (result.get("owner_email") and result.get("owner_phone")):
            try:
                oc = _find_person_contact(owner, name, city, state)
                if oc.get("email") and not result.get("owner_email"):
                    result["owner_email"] = oc["email"]
                if oc.get("phone") and not result.get("owner_phone"):
                    result["owner_phone"] = oc["phone"]
            except Exception:
                pass

        agent = result.get("registered_agent", "")
        if agent and not (result.get("reg_agent_email") and result.get("reg_agent_phone")):
            try:
                ac = _find_person_contact(agent, name, city, state)
                if ac.get("email") and not result.get("reg_agent_email"):
                    result["reg_agent_email"] = ac["email"]
                if ac.get("phone") and not result.get("reg_agent_phone"):
                    result["reg_agent_phone"] = ac["phone"]
            except Exception:
                pass

        if not result.get("general_email") and not result.get("owner_email"):
            slug = re.sub(r"[^a-z0-9]", "", (result.get("trade_name") or "business").lower())[:20]
            result["general_email"] = f"info@{slug}.com"

        return result

    enriched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_enrich_one, lead): i for i, lead in enumerate(leads)}
        for future in concurrent.futures.as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception:
                pass

    enriched.sort(key=lambda x: x.get("trade_name", ""))
    set_leads(enriched)
    _save_leads_to_file(enriched)
    return {"leads": enriched, "total": len(enriched), "saved": True}


def research_company(company_name, city="", website="") -> dict:
    """Deep-research a single company: Sunbiz, reviews, website, expansion score."""
    from tools.browser import sunbiz_lookup, scrape_website_contact, get_google_reviews

    result = {
        "_report_type": "company_research",
        "company_name": company_name,
        "city":         city,
        "date":         datetime.now().strftime("%b %d, %Y"),
    }

    try:
        sb = sunbiz_lookup(company_name)
        result["sunbiz"] = sb if isinstance(sb, dict) else {}
    except Exception:
        result["sunbiz"] = {}

    resolved_site   = website or result["sunbiz"].get("website", "")
    result["website"] = resolved_site

    if resolved_site:
        try:
            contact = scrape_website_contact(resolved_site)
            result["contact_data"] = contact if isinstance(contact, dict) else {}
        except Exception:
            result["contact_data"] = {}
    else:
        result["contact_data"] = {}

    try:
        query = f"{company_name} {city}".strip()
        gr    = get_google_reviews(query)
        result["google_rating"]  = gr.get("google_rating")
        result["google_reviews"] = gr.get("google_review_count")
    except Exception:
        result["google_rating"] = result["google_reviews"] = None

    exp = _compute_expansion_probability(result)
    result["expansion_probability"] = exp["score"]
    result["expansion_signals"]     = exp["signals"]
    return result
