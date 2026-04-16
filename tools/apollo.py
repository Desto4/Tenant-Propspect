"""Apollo.io organization + people search with email reveal.

Email-reveal credits are used; phone-reveal credits are never spent.
"""
import os
import re
from datetime import datetime

import requests

from core.state import get_leads, set_leads


def _norm_org_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def apollo_search_people(
    keywords=None, locations=None, num_results=20, _apollo_key=None
) -> dict:
    from core.state import set_leads

    apollo_key = _apollo_key or os.getenv("APOLLO_API_KEY", "")
    if not apollo_key:
        return {"error": "Apollo API key not configured. Please set it in Settings."}

    payload = {
        "page":                        1,
        "per_page":                    min(num_results or 20, 50),
        "q_organization_keyword_tags": [keywords] if keywords else [],
        "organization_locations":      locations or [],
    }
    headers = {
        "Content-Type":  "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key":     apollo_key,
    }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _org_id(org):
        return str(org.get("id") or org.get("organization_id") or "").strip()

    def _person_org_id(person):
        return str(
            person.get("organization_id")
            or (person.get("organization") or {}).get("id")
            or ""
        ).strip()

    def _person_name(person):
        name = (person.get("name") or "").strip()
        if name:
            return name
        first = (person.get("first_name") or "").strip()
        last  = (
            person.get("last_name") or person.get("last_name_obfuscated") or ""
        ).strip()
        return f"{first} {last}".strip()

    def _person_rank(person):
        title        = (person.get("title") or "").lower()
        email_status = (person.get("email_status") or person.get("email_status_cd") or "").lower()
        score = 0
        if person.get("has_email") or person.get("email"):
            score += 100
        if "verified" in email_status:
            score += 30
        elif "likely" in email_status:
            score += 20
        elif "unverified" in email_status:
            score += 10
        for marker, weight in [
            ("owner", 80), ("founder", 75), ("chief executive", 70), ("ceo", 70),
            ("president", 60), ("managing director", 55), ("principal", 55),
            ("partner", 55), ("co-owner", 55), ("head", 40),
            ("director", 35), ("manager", 25),
        ]:
            if marker in title:
                score += weight
                break
        if person.get("linkedin_url"):
            score += 5
        return score

    def _extract_email(person):
        email = (person.get("email") or "").strip()
        if email:
            return email
        for item in person.get("contact_emails") or []:
            candidate = (item.get("email") or item.get("value") or "").strip()
            if candidate:
                return candidate
        for item in person.get("emails") or []:
            if isinstance(item, dict):
                candidate = (item.get("email") or item.get("value") or "").strip()
            else:
                candidate = str(item).strip()
            if candidate:
                return candidate
        return ""

    def _enrich_people(selected_people):
        """Bulk-enrich up to 10 at a time; reveal emails only, never phones."""
        enriched, errors = {}, []
        for start in range(0, len(selected_people), 10):
            chunk   = selected_people[start:start + 10]
            details = [{"id": pid} for pid, _ in chunk if pid]
            if not details:
                continue
            try:
                resp = requests.post(
                    "https://api.apollo.io/api/v1/people/bulk_match",
                    params={
                        "reveal_personal_emails": "true",
                        "reveal_phone_number":    "false",
                        "run_waterfall_phone":    "false",
                    },
                    json={"details": details},
                    headers=headers,
                    timeout=30,
                )
                data    = resp.json()
                matches = data.get("matches") or []
                if resp.status_code >= 400:
                    errors.append(
                        f"Apollo enrichment failed (HTTP {resp.status_code}): {data}"
                    )
                    continue
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    pid = str(match.get("id") or "").strip()
                    if pid:
                        enriched[pid] = match
            except Exception as exc:
                errors.append(f"Apollo enrichment failed: {exc}")
        return enriched, errors

    def _fetch_people(orgs):
        org_ids = [_org_id(o) for o in orgs if _org_id(o)]
        if not org_ids:
            return {}, []

        org_name_to_id = {
            _norm_org_name(o.get("name", "")): _org_id(o)
            for o in orgs
            if _org_id(o) and _norm_org_name(o.get("name", ""))
        }

        params = [
            ("page", "1"),
            ("per_page", str(min(max(len(org_ids) * 3, 10), 100))),
            ("include_similar_titles", "true"),
        ]
        for oid in org_ids:
            params.append(("organization_ids[]", oid))
        for loc in locations or []:
            if loc:
                params.append(("organization_locations[]", loc))
        for status in ["verified", "likely to engage", "unverified"]:
            params.append(("contact_email_status[]", status))
        for seniority in ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager"]:
            params.append(("person_seniorities[]", seniority))
        if keywords:
            params.append(("q_keywords", keywords))

        try:
            resp  = requests.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                params=params, headers=headers, timeout=30,
            )
            data  = resp.json()
            people = data.get("people") or []
            if resp.status_code >= 400 or not isinstance(people, list):
                return {}, [f"Apollo people search failed (HTTP {resp.status_code}): {data}"]

            enriched, errors = _enrich_people([
                (str(p.get("id") or "").strip(), p) for p in people
            ])

            grouped = {}
            for person in people:
                if not isinstance(person, dict):
                    continue
                pid = str(person.get("id") or "").strip()
                merged = {**person, **(enriched.get(pid, {}))}

                matched_org_id = _person_org_id(merged)
                if not matched_org_id:
                    org_name = _norm_org_name(
                        (merged.get("organization") or {}).get("name", "")
                    )
                    matched_org_id = org_name_to_id.get(org_name, "")
                if not matched_org_id:
                    continue
                grouped.setdefault(matched_org_id, []).append(merged)

            selected = {
                oid: max(candidates, key=_person_rank)
                for oid, candidates in grouped.items()
            }
            return selected, errors
        except Exception as exc:
            return {}, [f"Apollo people search failed: {exc}"]

    # ── main ──────────────────────────────────────────────────────────────────

    try:
        r    = requests.post(
            "https://api.apollo.io/v1/organizations/search",
            json=payload, headers=headers, timeout=30,
        )
        data = r.json()
        if "organizations" not in data:
            return {"error": f"Apollo error (HTTP {r.status_code}): {data}"}

        organizations = data["organizations"]
        people_by_org, errors = _fetch_people(organizations)

        if errors:
            return {
                "error": (
                    "Apollo found organizations, but contact email reveal failed. "
                    + " ".join(errors)
                )
            }

        leads = []
        for org in organizations:
            oid         = _org_id(org)
            best_person = people_by_org.get(oid, {})
            phone       = org.get("phone") or ""
            if not phone:
                pp    = org.get("primary_phone") or {}
                phone = pp.get("sanitized_number") or pp.get("number") or ""
            fb_url       = org.get("facebook_url") or ""
            founded_year = org.get("founded_year") or ""
            formation_date  = f"01/01/{founded_year}" if founded_year else ""
            years_in_business = ""
            if founded_year:
                try:
                    years_in_business = str(datetime.now().year - int(founded_year))
                except Exception:
                    pass
            website     = org.get("website_url", "")
            apollo_email = _extract_email(best_person)
            owner_name   = _person_name(best_person)

            leads.append({
                "trade_name":        org.get("name", ""),
                "entity_name":       "",
                "formation_date":    formation_date,
                "years_in_business": years_in_business,
                "general_email":     "",
                "owner_name":        owner_name,
                "owner_email":       apollo_email,
                "owner_phone":       "",
                "registered_agent":  "",
                "reg_agent_address": "",
                "business_phone":    phone,
                "address":           org.get("raw_address", ""),
                "website":           website,
                "instagram_url":     "",
                "facebook_url":      fb_url,
                "google_review_count": "",
                "google_rating":     "",
                "industry":          org.get("industry", ""),
                "employees":         str(org.get("estimated_num_employees", "")),
                "linkedin_url":      best_person.get("linkedin_url") or org.get("linkedin_url", ""),
                "sunbiz_url":        "",
                "sunbiz_status":     "",
            })

        leads_with_email = [l for l in leads if l.get("owner_email")]
        if not leads_with_email:
            return {
                "error": (
                    "Apollo search returned organizations, but no contact emails were revealed. "
                    "Make sure the connected Apollo key has people enrichment/email reveal access."
                )
            }

        set_leads(leads_with_email)
        return {"leads": leads_with_email, "total": len(leads_with_email)}
    except Exception as e:
        return {"error": str(e)}
