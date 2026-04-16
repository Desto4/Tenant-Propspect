"""HubSpot CRM tools — create contacts and bulk-upload collected leads."""
import os
import re

import requests

from core.state import get_leads


def hubspot_create_contact(
    email, first_name="", last_name="", company="",
    phone="", website="", job_title="", linkedin="",
    _hubspot_token=None,
) -> dict:
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
            headers={
                "Authorization": f"Bearer {hubspot_token.strip()}",
                "Content-Type":  "application/json",
            },
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


def upload_leads_to_hubspot(_hubspot_token=None) -> dict:
    """Upload all collected leads to HubSpot in one call."""
    hubspot_token = _hubspot_token or os.getenv("HUBSPOT_TOKEN", "")
    if not hubspot_token:
        return {"error": "HubSpot token not configured. Please set it in Settings."}
    leads = get_leads()
    if not leads:
        return {"error": "No leads collected yet. Run a lead search first."}

    leads_with_email = [
        l for l in leads
        if l.get("owner_email") or l.get("general_email") or l.get("reg_agent_email")
    ]
    skipped_no_email = len(leads) - len(leads_with_email)
    results = {
        "uploaded": 0, "skipped": skipped_no_email, "errors": [],
        "contacts": [], "no_email_count": skipped_no_email,
    }

    for lead in leads_with_email:
        email = (
            lead.get("owner_email") or lead.get("general_email") or
            lead.get("reg_agent_email") or "johndoe@gmail.com"
        ).strip()
        raw_name = (lead.get("owner_name") or lead.get("registered_agent") or "").strip()
        if "@" in raw_name or re.match(
            r'^[\w._%+\-]+@[\w.\-]+\.[a-z]{2,}$', raw_name, re.IGNORECASE
        ):
            raw_name = ""
        first_name, last_name = "", ""
        if raw_name:
            if "," in raw_name:
                parts      = [p.strip().title() for p in raw_name.split(",", 1)]
                last_name  = parts[0]
                first_name = parts[1].split()[0] if parts[1] else ""
            else:
                parts      = raw_name.title().split()
                first_name = parts[0] if parts else ""
                last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
        if "@" in first_name: first_name = ""
        if "@" in last_name:  last_name  = ""

        company = (lead.get("trade_name") or lead.get("entity_name") or "").strip()
        phone   = (lead.get("owner_phone") or lead.get("business_phone") or "").strip()
        properties = {"email": email}
        if first_name:         properties["firstname"] = first_name
        if last_name:          properties["lastname"]  = last_name
        if company:            properties["company"]   = company
        if phone:              properties["phone"]     = phone
        if lead.get("website"): properties["website"] = lead["website"]
        properties["jobtitle"] = "Owner"
        try:
            r = requests.post(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                json={"properties": properties},
                headers={
                    "Authorization": f"Bearer {hubspot_token.strip()}",
                    "Content-Type":  "application/json",
                },
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
