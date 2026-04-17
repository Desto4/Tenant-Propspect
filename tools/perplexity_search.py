"""Perplexity-powered business search.

Uses Perplexity's Sonar models (which have live web search built in) to find
businesses across Yelp, Reddit, Google, local directories, and any other
public source — all in one call, without needing individual API keys or a
browser.

Returns leads in the standard lead shape so they can merge directly into
find_best_leads and feed into enrich_leads_batch.
"""
import json
import os
import re

_BLANK_LEAD = {
    "trade_name": "", "entity_name": "", "formation_date": "",
    "years_in_business": "", "sunbiz_status": "", "sunbiz_url": "",
    "general_email": "", "business_phone": "", "address": "",
    "city": "", "state": "", "website": "", "owner_name": "",
    "owner_email": "", "owner_phone": "", "registered_agent": "",
    "reg_agent_address": "", "reg_agent_email": "", "reg_agent_phone": "",
    "instagram_url": "", "facebook_url": "", "google_review_count": "",
    "google_rating": "", "yelp_rating": "", "yelp_review_count": "",
    "yelp_url": "", "industry": "", "employees": "", "linkedin_url": "",
    "source": "perplexity",
}


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of a Perplexity response."""
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    # Find outermost [ ... ]
    start = text.find("[")
    if start == -1:
        return []
    depth, i = 0, start
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
    try:
        return json.loads(text[start:i + 1])
    except Exception:
        return []


def _parse_perplexity_leads(raw_list: list, keyword: str) -> list:
    """Normalise Perplexity-returned objects into the standard lead shape."""
    leads = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = (
            item.get("name") or item.get("business_name") or
            item.get("trade_name") or ""
        ).strip()
        if not name:
            continue

        lead = dict(_BLANK_LEAD)
        lead["trade_name"]          = name
        lead["industry"]            = keyword
        lead["address"]             = item.get("address", "")
        lead["city"]                = item.get("city", "")
        lead["state"]               = item.get("state", "")
        lead["business_phone"]      = (
            item.get("phone") or item.get("business_phone") or ""
        )
        lead["website"]             = (
            item.get("website") or item.get("website_url") or ""
        )
        lead["general_email"]       = item.get("email", "")
        lead["google_rating"]       = str(item.get("google_rating", ""))
        lead["google_review_count"] = str(item.get("google_reviews", "") or
                                         item.get("google_review_count", ""))
        lead["yelp_rating"]         = str(item.get("yelp_rating", ""))
        lead["yelp_review_count"]   = str(item.get("yelp_reviews", "") or
                                         item.get("yelp_review_count", ""))
        lead["yelp_url"]            = item.get("yelp_url", "")
        lead["instagram_url"]       = item.get("instagram_url", "")
        lead["facebook_url"]        = item.get("facebook_url", "")
        lead["linkedin_url"]        = item.get("linkedin_url", "")
        leads.append(lead)
    return leads


def search_businesses_perplexity(
    keyword: str,
    location: str,
    num_results: int = 10,
    _perplexity_key: str = "",
) -> dict:
    """
    Use Perplexity Sonar to search across Yelp, Reddit, Google, local
    directories and any public source for businesses matching keyword + location.

    Returns the same lead shape as search_businesses_maps so results can
    merge into find_best_leads and feed directly into enrich_leads_batch.
    """
    perplexity_key = _perplexity_key or os.getenv("PERPLEXITY_API_KEY", "")
    if not perplexity_key:
        return {
            "leads":   [],
            "total":   0,
            "warning": "PERPLEXITY_API_KEY not configured. Add it to .env or Settings.",
        }

    prompt = f"""You are a business research assistant.

Find the top {num_results} {keyword} businesses in {location}.
Search across Google Maps, Yelp, Reddit recommendations, local directories,
and any other public sources. Prioritise businesses that appear on multiple
sources and have high ratings.

Return ONLY a JSON array — no prose, no markdown outside the array.
Each element must have these keys (use empty string "" for unknowns):
  name, address, city, state, phone, website, email,
  google_rating, google_reviews, yelp_rating, yelp_reviews, yelp_url,
  instagram_url, facebook_url

Example format:
[
  {{
    "name": "Acme Nail Salon",
    "address": "123 Main St",
    "city": "Miami",
    "state": "FL",
    "phone": "(305) 555-1234",
    "website": "https://acmenails.com",
    "email": "",
    "google_rating": "4.8",
    "google_reviews": "312",
    "yelp_rating": "4.5",
    "yelp_reviews": "204",
    "yelp_url": "https://yelp.com/biz/acme-nail-salon-miami",
    "instagram_url": "https://instagram.com/acmenails",
    "facebook_url": ""
  }}
]

Output the JSON array now:"""

    try:
        from openai import OpenAI as _OAI
        client = _OAI(api_key=perplexity_key, base_url="https://api.perplexity.ai/")
        response = client.chat.completions.create(
            model="sonar",
            messages=[
                {
                    "role":    "system",
                    "content": (
                        "You are a structured business data extraction tool. "
                        "Always respond with a raw JSON array only. "
                        "Do not include any explanation or markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = response.choices[0].message.content or ""
    except Exception as e:
        return {"leads": [], "total": 0, "error": f"Perplexity API error: {e}"}

    raw_list = _extract_json_array(text)
    if not raw_list:
        # Perplexity sometimes returns prose — attempt a light fallback parse
        raw_list = _fallback_parse(text, keyword, location)

    leads = _parse_perplexity_leads(raw_list, keyword)

    if not leads:
        return {
            "leads":   [],
            "total":   0,
            "warning": (
                f"Perplexity returned no structured results for '{keyword}' in '{location}'. "
                f"Raw response (first 400 chars): {text[:400]}"
            ),
        }

    return {"leads": leads, "total": len(leads)}


def _fallback_parse(text: str, keyword: str, location: str) -> list:
    """
    Light regex fallback when Perplexity returns prose instead of JSON.
    Extracts business names, addresses, and phone numbers mentioned in text.
    """
    leads = []

    # Business names often appear as "1. **Name**" or numbered list items
    names = re.findall(
        r'(?:^|\n)\s*\d+[\.\)]\s+(?:\*\*)?([A-Z][^\n\*]{3,60})(?:\*\*)?',
        text,
        re.MULTILINE,
    )
    phones = re.findall(r'\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}', text)
    ratings = re.findall(r'(\d[\.,]\d)\s*(?:stars?|★|out of 5)', text, re.IGNORECASE)
    websites = re.findall(r'https?://(?:www\.)?([^\s\)\]\"]+\.[a-z]{2,}(?:/\S*)?)', text)

    for i, name in enumerate(names[:20]):
        name = name.strip().rstrip('.,;:')
        if len(name) < 3:
            continue
        item = {
            "name":   name,
            "city":   location.split(",")[0].strip() if "," in location else location,
            "state":  location.split(",")[1].strip() if "," in location else "",
            "phone":  phones[i] if i < len(phones) else "",
            "google_rating": ratings[i].replace(",", ".") if i < len(ratings) else "",
            "website": f"https://{websites[i]}" if i < len(websites) else "",
        }
        leads.append(item)

    return leads
