"""
Tool functions, definitions (TOOLS), and TOOL_MAP for the MMG agent.

Shared mutable state
--------------------
_leads_store      -- enriched leads from the most recent search
_outreach_store   -- saved email drafts
_hunter_key       -- Hunter.io API key (set at runtime from session/env)
"""

import os
import re
import json
import csv
import io
import email as email_lib
import email.mime.text
import concurrent.futures
from datetime import datetime
from urllib.parse import quote_plus

import requests

from scrapers import (
    search_businesses_maps as _maps_search,
    sunbiz_lookup,
    scrape_website_contact,
    get_google_reviews,
    SCRAPE_HEADERS,
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_leads_store:    list = []
_outreach_store: list = []
_hunter_key:     str  = ""

# ---------------------------------------------------------------------------
# CSV field schema
# ---------------------------------------------------------------------------

LEAD_FIELDS = [
    "trade_name", "entity_name", "formation_date", "years_in_business",
    "sunbiz_status", "sunbiz_url",
    "general_email", "business_phone", "address", "website",
    "owner_name", "owner_email", "owner_phone",
    "registered_agent", "reg_agent_address", "reg_agent_email", "reg_agent_phone",
    "instagram_url", "facebook_url", "google_review_count", "google_rating",
    "industry", "employees", "linkedin_url",
]

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_businesses_maps",
        "description": (
            "PRIMARY lead discovery tool. Call this EXACTLY ONCE per lead generation request. "
            "Searches Google Maps for businesses by keyword and location using a real browser. "
            "Returns {leads: [...], total: N}. "
            "After receiving the result, call enrich_leads_batch in your NEXT tool call, "
            "passing result['leads'] as the leads parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":     {"type": "string", "description": "Business type, e.g. 'nail salon'"},
                "location":    {"type": "string", "description": "City and state, e.g. 'Miami, FL'"},
                "num_results": {"type": "integer", "description": "Number of businesses to return (default 10, max 20)", "default": 10},
            },
            "required": ["keyword", "location"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for any information — news, business details, market research, "
            "contact info, pricing, reviews, or anything else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "apollo_search_people",
        "description": (
            "Search for companies/organizations on Apollo.io by keyword and location. "
            "Only use this if the user explicitly asks for Apollo results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords":    {"type": "string", "description": "Industry keywords"},
                "locations":   {"type": "array", "items": {"type": "string"}},
                "num_results": {"type": "integer", "default": 20},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "enrich_leads_batch",
        "description": (
            "Step 2 of lead generation — call this after search_businesses_maps returns results. "
            "Pass the leads array from the search result. "
            "Enriches every lead with all 15 required fields via Sunbiz, website scrape, "
            "Google Maps reviews, and web search. "
            "NEVER call sunbiz_lookup / scrape_website_contact / get_google_reviews individually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "description": "Full lead objects from search_businesses_maps — pass unchanged",
                    "items": {"type": "object"},
                },
            },
            "required": ["leads"],
        },
    },
    {
        "name": "hubspot_create_contact",
        "description": "Create or update a contact in HubSpot CRM with full enriched lead data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email":      {"type": "string"},
                "first_name": {"type": "string"},
                "last_name":  {"type": "string"},
                "company":    {"type": "string"},
                "phone":      {"type": "string"},
                "website":    {"type": "string"},
                "job_title":  {"type": "string"},
                "linkedin":   {"type": "string"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "save_leads_csv",
        "description": "Save the fully enriched lead list to leads.csv.",
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["leads"],
        },
    },
    {
        "name": "get_collected_leads",
        "description": (
            "Return leads already collected and enriched in this session. "
            "Use when the user wants to view or work with previously found leads."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "upload_leads_to_hubspot",
        "description": (
            "Upload all collected leads to HubSpot CRM in one call. "
            "Handles all field mapping automatically — do NOT use hubspot_create_contact manually."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_outreach_csv",
        "description": "Save email drafts to outreach_drafts.csv.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drafts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":         {"type": "string"},
                            "email":        {"type": "string"},
                            "subject_line": {"type": "string"},
                            "email_body":   {"type": "string"},
                        },
                    },
                }
            },
            "required": ["drafts"],
        },
    },
    {
        "name": "send_gmail_email",
        "description": (
            "Send an email via the user's connected Gmail account. "
            "Only use when the user explicitly asks to SEND emails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to":              {"type": "string"},
                "subject":         {"type": "string"},
                "body":            {"type": "string"},
                "send_all_drafts": {"type": "boolean", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "research_company",
        "description": (
            "Deep-research a single company. Use when the user asks to 'research', 'look up', "
            "'find everything about', or 'deep dive' on a specific business."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string"},
                "city":         {"type": "string"},
                "website":      {"type": "string"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "create_gmail_drafts",
        "description": (
            "Create Gmail drafts from the outreach emails so the user can review and send "
            "them manually from Gmail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_all": {"type": "boolean", "default": True},
                "to":        {"type": "string"},
                "subject":   {"type": "string"},
                "body":      {"type": "string"},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def search_businesses_maps(keyword, location, num_results=10):
    global _leads_store
    result = _maps_search(keyword, location, num_results)
    if "leads" in result:
        _leads_store = result["leads"]
    return result


def web_search(query):
    try:
        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(f"Summary: {data['AbstractText']}")
            if data.get("AbstractURL"):
                results.append(f"Source: {data['AbstractURL']}")
        for topic in data.get("RelatedTopics", [])[:6]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"- {topic['Text']}")
                if topic.get("FirstURL"):
                    results.append(f"  {topic['FirstURL']}")
        if data.get("Answer"):
            results.append(f"Answer: {data['Answer']}")
        if not results:
            results.append(f"No direct results. Try: https://www.google.com/search?q={quote_plus(query)}")
        return {"results": "\n".join(results), "query": query}
    except Exception as e:
        return {"error": str(e)}


def _hunter_domain_search(domain):
    """Silently look up emails for a domain via Hunter.io. Returns best email or ''."""
    global _hunter_key
    if not _hunter_key or not domain:
        return ""
    domain = re.sub(r'^https?://', '', domain).split('/')[0].split('?')[0].strip()
    if not domain:
        return ""
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": _hunter_key, "limit": 10},
            timeout=10,
        )
        emails = r.json().get("data", {}).get("emails", [])
        if not emails:
            return ""
        emails.sort(
            key=lambda e: (1 if e.get("type") in ("generic", "personal") else 0, e.get("confidence", 0)),
            reverse=True,
        )
        return emails[0].get("value", "")
    except Exception:
        return ""


def apollo_search_people(keywords=None, locations=None, num_results=20, _apollo_key=None):
    global _leads_store
    apollo_key = _apollo_key or os.getenv("APOLLO_API_KEY", "")
    if not apollo_key:
        return {"error": "Apollo API key not configured. Please set it in Settings."}

    payload = {
        "page": 1,
        "per_page": min(num_results or 20, 50),
        "q_organization_keyword_tags": [keywords] if keywords else [],
        "organization_locations": locations or [],
    }
    try:
        r = requests.post(
            "https://api.apollo.io/v1/organizations/search",
            json=payload,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": apollo_key},
            timeout=30,
        )
        data = r.json()
        if "organizations" not in data:
            return {"error": f"Apollo error (HTTP {r.status_code}): {data}"}

        leads = []
        for org in data["organizations"]:
            phone = org.get("phone") or ""
            if not phone:
                pp = org.get("primary_phone") or {}
                phone = pp.get("sanitized_number") or pp.get("number") or ""
            founded_year = org.get("founded_year") or ""
            formation_date = f"01/01/{founded_year}" if founded_year else ""
            years_in_business = ""
            if founded_year:
                try:
                    years_in_business = str(datetime.now().year - int(founded_year))
                except Exception:
                    pass
            website = org.get("website_url", "")
            leads.append({
                "trade_name": org.get("name", ""), "entity_name": "",
                "formation_date": formation_date, "years_in_business": years_in_business,
                "general_email": _hunter_domain_search(website) if website else "",
                "owner_name": "", "owner_email": "", "owner_phone": "",
                "registered_agent": "", "reg_agent_address": "",
                "business_phone": phone, "address": org.get("raw_address", ""),
                "website": website, "instagram_url": "",
                "facebook_url": org.get("facebook_url") or "",
                "google_review_count": "", "google_rating": "",
                "industry": org.get("industry", ""),
                "employees": str(org.get("estimated_num_employees", "")),
                "linkedin_url": org.get("linkedin_url", ""),
                "sunbiz_url": "", "sunbiz_status": "",
            })

        _leads_store = leads
        _save_leads_to_file(leads)
        return {"leads": leads, "total": len(leads)}
    except Exception as e:
        return {"error": str(e)}


def hubspot_create_contact(email, first_name="", last_name="", company="",
                            phone="", website="", job_title="", linkedin="",
                            _hubspot_token=None):
    hubspot_token = _hubspot_token or os.getenv("HUBSPOT_TOKEN", "")
    if not hubspot_token:
        return {"error": "HubSpot token not configured. Please set it in Settings."}
    properties = {"email": email}
    if first_name: properties["firstname"]    = first_name
    if last_name:  properties["lastname"]     = last_name
    if company:    properties["company"]      = company
    if phone:      properties["phone"]        = phone
    if website:    properties["website"]      = website
    if job_title:  properties["jobtitle"]     = job_title
    if linkedin:   properties["linkedin_bio"] = linkedin
    try:
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            json={"properties": properties},
            headers={"Authorization": f"Bearer {hubspot_token.strip()}", "Content-Type": "application/json"},
            timeout=20,
        )
        data = r.json()
        if r.status_code in (200, 201):
            return {"success": True, "id": data.get("id"), "email": email, "company": company}
        elif r.status_code == 409:
            return {"success": False, "error": "Contact already exists", "email": email}
        else:
            return {"success": False, "error": data.get("message", str(data)), "email": email}
    except Exception as e:
        return {"error": str(e)}


def get_collected_leads():
    global _leads_store
    if not _leads_store:
        return {"leads": [], "count": 0, "message": "No leads collected yet in this session."}
    return {"leads": _leads_store, "count": len(_leads_store)}


def upload_leads_to_hubspot(_hubspot_token=None):
    global _leads_store
    hubspot_token = _hubspot_token or os.getenv("HUBSPOT_TOKEN", "")
    if not hubspot_token:
        return {"error": "HubSpot token not configured. Please set it in Settings."}
    if not _leads_store:
        return {"error": "No leads collected yet. Run a lead search first."}

    leads_with_email = [
        l for l in _leads_store
        if l.get("owner_email") or l.get("general_email") or l.get("reg_agent_email")
    ]
    results = {"uploaded": 0, "skipped": len(_leads_store) - len(leads_with_email),
               "errors": [], "contacts": [], "no_email_count": len(_leads_store) - len(leads_with_email)}

    for lead in leads_with_email:
        email = (lead.get("owner_email") or lead.get("general_email") or
                 lead.get("reg_agent_email") or "johndoe@gmail.com").strip()
        raw_name = (lead.get("owner_name") or lead.get("registered_agent") or "").strip()
        if "@" in raw_name:
            raw_name = ""
        first_name, last_name = "", ""
        if raw_name:
            if "," in raw_name:
                parts = [p.strip().title() for p in raw_name.split(",", 1)]
                last_name = parts[0]
                first_name = parts[1].split()[0] if parts[1] else ""
            else:
                parts = raw_name.title().split()
                first_name = parts[0] if parts else ""
                last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
        if "@" in first_name: first_name = ""
        if "@" in last_name:  last_name  = ""

        company = (lead.get("trade_name") or lead.get("entity_name") or "").strip()
        phone   = (lead.get("owner_phone") or lead.get("business_phone") or "").strip()

        properties = {"email": email}
        if first_name: properties["firstname"] = first_name
        if last_name:  properties["lastname"]  = last_name
        if company:    properties["company"]   = company
        if phone:      properties["phone"]     = phone
        if lead.get("website"): properties["website"] = lead["website"]
        properties["jobtitle"] = "Owner"

        try:
            r = requests.post(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                json={"properties": properties},
                headers={"Authorization": f"Bearer {hubspot_token.strip()}", "Content-Type": "application/json"},
                timeout=20,
            )
            data = r.json()
            if r.status_code in (200, 201):
                results["uploaded"] += 1
                results["contacts"].append({"email": email, "company": company, "id": data.get("id")})
            elif r.status_code == 409:
                results["skipped"] += 1
                results["errors"].append(f"Already exists: {email}")
            else:
                results["skipped"] += 1
                results["errors"].append(f"{email}: {data.get('message', str(r.status_code))}")
        except Exception as e:
            results["skipped"] += 1
            results["errors"].append(f"{email}: {str(e)}")

    return results


def save_leads_csv(leads):
    global _leads_store
    _leads_store = leads
    _save_leads_to_file(leads)
    return {"success": True, "count": len(leads), "path": "leads.csv"}


def _save_leads_to_file(leads):
    try:
        path = os.path.join(os.path.dirname(__file__), "leads.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LEAD_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(leads)
    except Exception:
        pass


def save_outreach_csv(drafts):
    global _outreach_store
    _outreach_store = drafts
    try:
        path = os.path.join(os.path.dirname(__file__), "outreach_drafts.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "email", "subject_line", "email_body"],
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(drafts)
        return {"success": True, "path": path, "count": len(drafts), "drafts": drafts}
    except Exception as e:
        return {"error": str(e)}


def _get_gmail_creds():
    """Get Gmail address + app password from session → .env → file."""
    import flask
    _file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gmail_app.json")
    try:
        addr = flask.session.get("gmail_address", "")
        pw   = flask.session.get("gmail_app_password", "")
    except RuntimeError:
        addr, pw = "", ""
    addr = addr or os.getenv("GMAIL_ADDRESS", "")
    pw   = pw   or os.getenv("GMAIL_APP_PASSWORD", "")
    if (not addr or not pw) and os.path.exists(_file):
        try:
            with open(_file) as f:
                data = json.load(f)
            addr = addr or data.get("gmail_address", "")
            pw   = pw   or data.get("gmail_app_password", "")
        except Exception:
            pass
    return addr, pw


def send_gmail_email(to=None, subject=None, body=None, send_all_drafts=False):
    import smtplib
    addr, pw = _get_gmail_creds()
    if not addr or not pw:
        return {"error": "Gmail not configured. Add your Gmail address and App Password in Settings."}

    def _send_one(to_addr, subj, text_body):
        msg = email_lib.mime.text.MIMEText(text_body, "plain")
        msg["From"] = addr; msg["To"] = to_addr; msg["Subject"] = subj
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(addr, pw)
            srv.send_message(msg)

    if send_all_drafts:
        global _outreach_store
        if not _outreach_store:
            return {"error": "No outreach drafts found. Write outreach emails first."}
        sent, failed = [], []
        for d in _outreach_store:
            try:
                _send_one(d.get("email", ""), d.get("subject_line", "No subject"), d.get("email_body", ""))
                sent.append(d.get("email", ""))
            except Exception as ex:
                failed.append({"email": d.get("email", ""), "error": str(ex)})
        return {"success": True, "sent_count": len(sent), "failed_count": len(failed),
                "sent": sent, "failed": failed}
    else:
        if not to or not subject or not body:
            return {"error": "Missing required fields: to, subject, body"}
        try:
            _send_one(to, subject, body)
            return {"success": True, "to": to}
        except Exception as e:
            return {"error": f"Gmail send failed: {str(e)}"}


def create_gmail_drafts(draft_all=True, to=None, subject=None, body=None):
    import imaplib
    addr, pw = _get_gmail_creds()
    if not addr or not pw:
        return {"error": "Gmail not configured. Add your Gmail address and App Password in Settings."}

    def _append(to_addr, subj, text_body):
        msg = email_lib.mime.text.MIMEText(text_body, "plain")
        msg["From"] = addr; msg["To"] = to_addr; msg["Subject"] = subj
        raw = msg.as_bytes()
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(addr, pw)
        imap.append("[Gmail]/Drafts", "\\Draft",
                    imaplib.Time2Internaldate(datetime.now().timestamp()), raw)
        imap.logout()

    if draft_all:
        global _outreach_store
        if not _outreach_store:
            return {"error": "No outreach drafts found. Write outreach emails first."}
        created, failed = [], []
        for d in _outreach_store:
            try:
                _append(d.get("email", ""), d.get("subject_line", "No subject"), d.get("email_body", ""))
                created.append(d.get("email", ""))
            except Exception as ex:
                failed.append({"email": d.get("email", ""), "error": str(ex)})
        return {"success": True, "created_count": len(created), "failed_count": len(failed),
                "message": f"Saved {len(created)} email(s) to your Gmail Drafts folder."}
    else:
        if not to or not subject or not body:
            return {"error": "Missing required fields: to, subject, body"}
        try:
            _append(to, subject, body)
            return {"success": True, "to": to, "message": "Draft saved to your Gmail Drafts folder."}
        except Exception as e:
            return {"error": f"Gmail draft creation failed: {str(e)}"}


def _find_person_contact(name, business_name="", city="", state=""):
    """Web-search for a person's email and phone. Returns {"email": ..., "phone": ...}."""
    if not name:
        return {"email": "", "phone": ""}
    noise = {"example", "sentry", "wixpress", "squarespace", "domain", "noreply",
             "wordpress", "schema", "w3.org", "google", "yelp", "facebook"}

    def _extract(text):
        email = ""
        for e in re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', text):
            if not any(n in e.lower() for n in noise) and len(e) < 60:
                email = e.lower()
                break
        phones = re.findall(r'\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}', text)
        return email, phones[0].strip() if phones else ""

    queries = []
    if business_name:
        queries.append(f'"{name}" "{business_name}" email contact')
        queries.append(f'"{name}" {city} {state} {business_name} owner email')
    queries.append(f'"{name}" {city} {state} email phone')

    for q in queries:
        try:
            result = web_search(q.strip())
            email, phone = _extract(result.get("results", ""))
            if email or phone:
                return {"email": email, "phone": phone}
        except Exception:
            continue
    return {"email": "", "phone": ""}


def enrich_leads_batch(leads=None):
    """Enrich every lead with Sunbiz, website contact, Google reviews, and owner contact."""
    global _leads_store
    if not leads:
        leads = list(_leads_store)
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

        # 1. Sunbiz
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

        # 2. Website scrape
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

        # 2b. Hunter.io fallback
        if url and not result.get("general_email"):
            hunter_email = _hunter_domain_search(url)
            if hunter_email:
                result["general_email"] = hunter_email

        # 3. Google reviews (skip if already populated from Maps)
        if not (result.get("google_rating") and result.get("google_review_count")):
            try:
                gr = get_google_reviews(name, city, state)
                if gr.get("google_rating"):
                    result["google_rating"] = gr["google_rating"]
                if gr.get("google_review_count"):
                    result["google_review_count"] = gr["google_review_count"]
            except Exception:
                pass

        # 4. Owner contact
        owner = result.get("owner_name", "")
        if owner and not (result.get("owner_email") and result.get("owner_phone")):
            oc = _find_person_contact(owner, name, city, state)
            if oc.get("email") and not result.get("owner_email"):
                result["owner_email"] = oc["email"]
            if oc.get("phone") and not result.get("owner_phone"):
                result["owner_phone"] = oc["phone"]

        # 5. Registered agent contact
        agent = result.get("registered_agent", "")
        if agent and not (result.get("reg_agent_email") and result.get("reg_agent_phone")):
            ac = _find_person_contact(agent, name, city, state)
            if ac.get("email") and not result.get("reg_agent_email"):
                result["reg_agent_email"] = ac["email"]
            if ac.get("phone") and not result.get("reg_agent_phone"):
                result["reg_agent_phone"] = ac["phone"]

        # Placeholder email if nothing found
        if not result.get("general_email") and not result.get("owner_email"):
            slug = re.sub(r"[^a-z0-9]", "", (result.get("trade_name") or "business").lower())[:20]
            result["general_email"] = f"info@{slug}.com"

        return result

    enriched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for future in concurrent.futures.as_completed(
            {executor.submit(_enrich_one, lead): i for i, lead in enumerate(leads)}
        ):
            try:
                enriched.append(future.result())
            except Exception:
                pass

    enriched.sort(key=lambda x: x.get("trade_name", ""))
    _leads_store = enriched
    _save_leads_to_file(enriched)
    return {"leads": enriched, "total": len(enriched), "saved": True}


def research_company(company_name, city="", website=""):
    """Deep-research a single company: Sunbiz + reviews + website + expansion score."""
    result = {
        "_report_type": "company_research",
        "company_name": company_name,
        "city": city,
        "date": datetime.now().strftime("%b %d, %Y"),
    }
    try:
        sb = sunbiz_lookup(company_name)
        result["sunbiz"] = sb if isinstance(sb, dict) else {}
    except Exception:
        result["sunbiz"] = {}

    resolved_site = website or result["sunbiz"].get("website", "")
    result["website"] = resolved_site

    if resolved_site:
        try:
            result["contact_data"] = scrape_website_contact(resolved_site) or {}
        except Exception:
            result["contact_data"] = {}
    else:
        result["contact_data"] = {}

    try:
        gr = get_google_reviews(f"{company_name} {city}".strip())
        result["google_rating"]  = gr.get("google_rating")
        result["google_reviews"] = gr.get("google_review_count")
    except Exception:
        result["google_rating"] = result["google_reviews"] = None

    exp = _compute_expansion_probability(result)
    result["expansion_probability"] = exp["score"]
    result["expansion_signals"]     = exp["signals"]
    return result


def _compute_expansion_probability(data):
    """Return {"score": 0-99, "signals": [...]}."""
    score, signals = 35, []
    sb = data.get("sunbiz", {})
    cd = data.get("contact_data", {})

    if "active" in (sb.get("status") or sb.get("sunbiz_status") or "").lower():
        score += 10

    rating, reviews = 0.0, 0
    try: rating  = float(data.get("google_rating") or 0)
    except Exception: pass
    try: reviews = int(str(data.get("google_reviews") or 0).replace(",", ""))
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
                from datetime import datetime as _dt
                years = int((_dt.now() - _dt.strptime(fd, "%m/%d/%Y")).days / 365)
        except Exception:
            pass

    if years >= 5:
        score += 15
        signals.append(f"{years}-year operational history demonstrates business stability")
    elif years >= 3:
        score += 8
        signals.append(f"{years} years in business — approaching prime expansion window")

    if data.get("website"):    score += 5
    if cd.get("instagram_url"):
        score += 8
        signals.append("Active social media presence indicates marketing momentum")
    if cd.get("facebook_url"): score += 3

    owner = sb.get("owner_name") or ""
    if owner:
        score += 5
        signals.append(f"Owner {owner.title()} identified — direct decision-maker outreach possible")
    if sb.get("owner_email") or cd.get("owner_email") or sb.get("owner_phone") or cd.get("owner_phone"):
        score += 5
        signals.append("Direct owner contact info verified — high response probability")

    if len(signals) < 3:
        signals.append("Single-location operation — minimal competitive risk from multi-location chains")

    return {"score": min(96, max(25, score)), "signals": signals[:6]}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_MAP = {
    "search_businesses_maps":  search_businesses_maps,
    "web_search":              web_search,
    "apollo_search_people":    apollo_search_people,
    "enrich_leads_batch":      enrich_leads_batch,
    "get_collected_leads":     get_collected_leads,
    "upload_leads_to_hubspot": upload_leads_to_hubspot,
    "sunbiz_lookup":           sunbiz_lookup,
    "scrape_website_contact":  scrape_website_contact,
    "get_google_reviews":      get_google_reviews,
    "hubspot_create_contact":  hubspot_create_contact,
    "save_leads_csv":          save_leads_csv,
    "save_outreach_csv":       save_outreach_csv,
    "send_gmail_email":        send_gmail_email,
    "create_gmail_drafts":     create_gmail_drafts,
    "research_company":        research_company,
}


def run_tool(name, inputs, apollo_key="", hubspot_token=""):
    fn = TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    kwargs = dict(inputs)
    if name == "apollo_search_people":
        kwargs["_apollo_key"] = apollo_key
    elif name in ("hubspot_create_contact", "upload_leads_to_hubspot"):
        kwargs["_hubspot_token"] = hubspot_token
    return fn(**kwargs)
