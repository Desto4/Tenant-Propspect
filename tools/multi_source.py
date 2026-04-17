"""Multi-source lead discovery and ranking.

`find_best_leads` pulls from Google Maps, Yelp, and Reddit in parallel,
merges duplicate businesses across sources, scores each one by quality
signals, and returns the top N.

Ranking signals:
- cross-source presence (appearing on more sources = more credible)
- Google rating weighted by review count
- Yelp rating weighted by review count
- Reddit buzz (mentions + upvotes)
- completeness of data (phone, website, address)
"""
import concurrent.futures
import re

import requests

from core.state import set_leads

from tools.leads import enrich_leads_sunbiz_only

# Cache Nominatim results per process (avoid repeat lookups for the same location string).
_GEOCODE_FL_CACHE: dict[str, bool] = {}


# ── Normalization / dedup helpers ─────────────────────────────────────────────

_NAME_NOISE = re.compile(r"[^a-z0-9]")


def _norm_name(name: str) -> str:
    """Collapse to lowercase alphanumeric for fuzzy matching."""
    return _NAME_NOISE.sub("", (name or "").lower())


def _norm_phone(phone: str) -> str:
    """Keep only digits. Last 10 digits identifies a US phone uniquely."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _merge_lead(primary: dict, secondary: dict) -> dict:
    """Merge secondary into primary — primary wins when fields conflict."""
    out = dict(primary)
    for k, v in secondary.items():
        if not v:
            continue
        if not out.get(k):
            out[k] = v
    # Track which sources contributed
    primary_sources   = set(primary.get("sources") or [primary.get("source")] if primary.get("source") else [])
    secondary_sources = set(secondary.get("sources") or [secondary.get("source")] if secondary.get("source") else [])
    sources = {s for s in (primary_sources | secondary_sources) if s}
    out["sources"] = sorted(sources)
    return out


# ── Ranking ───────────────────────────────────────────────────────────────────

def _score_lead(lead: dict, reddit_mentions: dict) -> int:
    """Return a 0-100 quality score based on signals."""
    score = 0

    # Multi-source presence — each extra source adds credibility
    sources = lead.get("sources") or []
    score  += min(len(sources), 3) * 10      # up to +30

    # Google rating × review count
    try:
        g_rating  = float(lead.get("google_rating") or 0)
        g_reviews = int(str(lead.get("google_review_count") or 0).replace(",", ""))
        if g_rating >= 4.5 and g_reviews >= 100:
            score += 25
        elif g_rating >= 4.5:
            score += 18
        elif g_rating >= 4.0 and g_reviews >= 200:
            score += 18
        elif g_rating >= 4.0:
            score += 10
        elif g_rating >= 3.5:
            score += 5
        if g_reviews >= 500:
            score += 8
        elif g_reviews >= 100:
            score += 4
    except Exception:
        pass

    # Yelp rating × review count
    try:
        y_rating  = float(lead.get("yelp_rating") or 0)
        y_reviews = int(str(lead.get("yelp_review_count") or 0).replace(",", ""))
        if y_rating >= 4.5 and y_reviews >= 50:
            score += 15
        elif y_rating >= 4.5:
            score += 10
        elif y_rating >= 4.0:
            score += 6
        if y_reviews >= 200:
            score += 5
    except Exception:
        pass

    # Reddit mentions — people talking about it is a strong positive signal
    name_key = _norm_name(lead.get("trade_name", ""))
    mentions = reddit_mentions.get(name_key, 0)
    if mentions >= 3:
        score += 15
    elif mentions >= 1:
        score += 8

    # Data completeness (easier to contact = more valuable)
    if lead.get("website"):        score += 3
    if lead.get("business_phone"): score += 3
    if lead.get("address"):        score += 2

    return min(score, 100)


def _location_looks_florida(location: str) -> bool:
    """Heuristic: Florida Sunbiz only applies to FL businesses."""
    if not (location or "").strip():
        return False
    s = location.upper()
    if re.search(r"\bFL\b", s) or "FLORIDA" in s:
        return True
    # Common FL metros / counties without explicit state in the query
    hints = (
        "MIAMI", "DADE", "MIAMI-DADE", "BROWARD", "PALM BEACH", "FORT LAUDERDALE", "WEST PALM",
        "BOCA RATON", "DELRAY", "DEERFIELD", "PLANTATION", "PEMBROKE PINES", "HIALEAH",
        "ORLANDO", "TAMPA", "ST. PETERSBURG", "ST PETERSBURG", "JACKSONVILLE", "SARASOTA",
        "NAPLES", "FORT MYERS", "KEY WEST", "GAINESVILLE", "TALLAHASSEE",
        "CLEARWATER", "CORAL GABLES", "SOUTH FLORIDA", "TREASURE COAST", "SPACE COAST",
        "PANHANDLE", "OCALA", "PENSACOLA", "MELBOURNE", "LAKELAND", "KISSIMMEE",
    )
    return any(h in s for h in hints)


def _lead_list_suggests_florida(leads: list) -> bool:
    """True if ranked leads look like FL (Maps/Yelp often set state/address even when the user omits FL)."""
    for lead in leads or []:
        st = (lead.get("state") or "").strip().upper()
        if st == "FL":
            return True
        addr = (lead.get("address") or "") + " " + (lead.get("city") or "")
        if re.search(r"\bFL\b", addr.upper()) or ", FL " in (lead.get("address") or "").upper():
            return True
    return False


def _location_geocodes_to_florida(location: str) -> bool:
    """Resolve the free-text location via OpenStreetMap Nominatim; True if the top hit is in Florida.

    This covers city-only queries (e.g. 'Winter Haven') without maintaining a huge city list.
    Uses a small in-memory cache. On failure or ambiguous results, returns False.
    """
    q = (location or "").strip()
    if not q:
        return False
    key = q.lower()
    if key in _GEOCODE_FL_CACHE:
        return _GEOCODE_FL_CACHE[key]

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q":             f"{q}, USA",
                "format":        "json",
                "limit":         1,
                "addressdetails": 1,
            },
            headers={
                "User-Agent": (
                    "TenantProspect/1.0 (https://github.com/Desto4/Tenant-Propspect; "
                    "Florida Sunbiz location check)"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        if r.status_code != 200:
            _GEOCODE_FL_CACHE[key] = False
            return False
        data = r.json()
        if not data:
            _GEOCODE_FL_CACHE[key] = False
            return False
        addr = (data[0].get("address") or {}) if isinstance(data[0], dict) else {}
        state = (addr.get("state") or "").strip().lower()
        # Nominatim US: state is often full name
        if state in ("florida", "fl"):
            _GEOCODE_FL_CACHE[key] = True
            return True
        # ISO/state_code style when present
        sc = (addr.get("ISO3166-2-lvl4") or "").upper()
        if sc == "US-FL":
            _GEOCODE_FL_CACHE[key] = True
            return True
        disp = (data[0].get("display_name") or "")
        if re.search(r",\s*FL\s*,", disp) or ", Florida," in disp:
            _GEOCODE_FL_CACHE[key] = True
            return True
        _GEOCODE_FL_CACHE[key] = False
        return False
    except Exception:
        _GEOCODE_FL_CACHE[key] = False
        return False


def _should_run_sunbiz(location: str, ranked_leads: list) -> bool:
    """Run Florida registry lookup when the query, geocoding, or lead rows indicate Florida."""
    if _location_looks_florida(location):
        return True
    if _lead_list_suggests_florida(ranked_leads):
        return True
    return _location_geocodes_to_florida(location)


def _build_reddit_mention_map(posts: list, candidate_names: list) -> dict:
    """Count how many Reddit posts mention each business by normalised name."""
    if not posts or not candidate_names:
        return {}

    # Prepare: for each candidate, normalised name + words-set for fuzzy match
    candidates = []
    for name in candidate_names:
        norm  = _norm_name(name)
        words = set(re.findall(r"\w+", (name or "").lower()))
        # Filter stop words and 2-letter tokens to reduce false positives
        words = {w for w in words if len(w) >= 3}
        if norm and words:
            candidates.append((norm, words))

    counts = {}
    for post in posts:
        text = (post.get("title", "") + " " + post.get("snippet", "")).lower()
        if not text.strip():
            continue
        text_norm  = _norm_name(text)
        text_words = set(re.findall(r"\w+", text))
        for norm, words in candidates:
            # Exact substring on normalised name OR all tokens present
            if norm in text_norm or words.issubset(text_words):
                counts[norm] = counts.get(norm, 0) + 1
    return counts


# ── Main tool ─────────────────────────────────────────────────────────────────

def find_best_leads(
    keyword: str,
    location: str,
    num_results: int = 10,
    sources: list = None,
    enrich_sunbiz: bool = True,
) -> dict:
    """
    Search across multiple sources, merge duplicates, rank by quality, return the top N.

    Parameters:
        keyword:     business type (e.g. 'nail salon')
        location:    city + state (e.g. 'Miami, FL')
        num_results: how many top-ranked leads to return (default 10)
        sources:     optional list of sources to include;
                     default ['maps', 'yelp', 'reddit', 'perplexity']
        enrich_sunbiz: when True and the location looks like Florida, run Florida
                     Division of Corporations (Sunbiz) lookup on each ranked lead
                     so entity status and officers are filled before enrichment.

    Returns:
        {
            "leads":   [...ranked leads with 'sources' and 'quality_score'...],
            "total":   N,
            "breakdown": {"maps": int, "yelp": int, "perplexity": int, "reddit_mentions": int},
            "warnings": [str, ...],
        }
    """
    from tools.browser import search_businesses_maps, search_businesses_yelp, search_reddit
    from tools.perplexity_search import search_businesses_perplexity

    sources  = sources or ["maps", "yelp", "reddit", "perplexity"]
    warnings = []

    # Pull a wider net than num_results so ranking has room to pick winners.
    fetch_n = max(num_results * 2, 10)

    # ── 1. Fan out to all sources in parallel ────────────────────────────────
    tasks = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        if "maps" in sources:
            tasks["maps"] = ex.submit(search_businesses_maps, keyword, location, fetch_n)
        if "yelp" in sources:
            tasks["yelp"] = ex.submit(search_businesses_yelp, keyword, location, fetch_n)
        if "perplexity" in sources:
            tasks["perplexity"] = ex.submit(
                search_businesses_perplexity, keyword, location, fetch_n
            )
        if "reddit" in sources:
            # Reddit query combines keyword + location for local intelligence
            tasks["reddit"] = ex.submit(
                search_reddit,
                f"{keyword} {location}",
                None,
                fetch_n * 2,
            )

        results = {name: future.result() for name, future in tasks.items()}

    # ── 2. Collect business-style leads (maps + yelp + perplexity) ───────────
    maps_leads       = []
    yelp_leads       = []
    perplexity_leads = []

    if "maps" in results:
        mr = results["maps"]
        if isinstance(mr, dict):
            if mr.get("warning"):  warnings.append(f"Maps: {mr['warning']}")
            if mr.get("error"):    warnings.append(f"Maps error: {mr['error']}")
            for lead in mr.get("leads", []):
                lead["source"] = "maps"
                maps_leads.append(lead)

    if "yelp" in results:
        yr = results["yelp"]
        if isinstance(yr, dict):
            if yr.get("warning"):  warnings.append(f"Yelp: {yr['warning']}")
            if yr.get("error"):    warnings.append(f"Yelp error: {yr['error']}")
            for lead in yr.get("leads", []):
                lead["source"] = "yelp"
                yelp_leads.append(lead)

    if "perplexity" in results:
        pr = results["perplexity"]
        if isinstance(pr, dict):
            if pr.get("warning"):  warnings.append(f"Perplexity: {pr['warning']}")
            if pr.get("error"):    warnings.append(f"Perplexity error: {pr['error']}")
            for lead in pr.get("leads", []):
                lead["source"] = "perplexity"
                perplexity_leads.append(lead)

    # ── 3. Reddit: used as a signal layer, not leads ─────────────────────────
    reddit_posts = []
    if "reddit" in results:
        rr = results["reddit"]
        if isinstance(rr, dict):
            if rr.get("warning"):  warnings.append(f"Reddit: {rr['warning']}")
            if rr.get("error"):    warnings.append(f"Reddit error: {rr['error']}")
            reddit_posts = rr.get("posts", []) or []

    # ── 4. Merge duplicates across all business sources ───────────────────────
    # Index by normalised name; fall back to phone digits for extra matching
    merged = {}

    def _key(lead: dict) -> str:
        name_key  = _norm_name(lead.get("trade_name", ""))
        phone_key = _norm_phone(lead.get("business_phone", ""))
        return name_key or phone_key or ""

    # Also track phone→name-key so a lead appearing with different names but
    # same phone merges correctly
    phone_to_key = {}

    for lead in maps_leads + yelp_leads + perplexity_leads:
        k = _key(lead)
        if not k:
            continue
        phone_k = _norm_phone(lead.get("business_phone", ""))
        if phone_k and phone_k in phone_to_key:
            k = phone_to_key[phone_k]
        elif phone_k:
            phone_to_key[phone_k] = k

        if k in merged:
            merged[k] = _merge_lead(merged[k], lead)
        else:
            lead["sources"] = [lead.get("source", "")] if lead.get("source") else []
            merged[k] = lead

    # ── 5. Reddit mention count per candidate ────────────────────────────────
    candidate_names   = [l.get("trade_name", "") for l in merged.values()]
    reddit_mentions   = _build_reddit_mention_map(reddit_posts, candidate_names)
    reddit_total_hits = sum(reddit_mentions.values())

    # ── 6. Score and rank ─────────────────────────────────────────────────────
    ranked = []
    for lead in merged.values():
        name_key = _norm_name(lead.get("trade_name", ""))
        lead["quality_score"]    = _score_lead(lead, reddit_mentions)
        lead["reddit_mentions"]  = reddit_mentions.get(name_key, 0)
        lead.setdefault("sources", [])
        ranked.append(lead)

    ranked.sort(key=lambda l: l.get("quality_score", 0), reverse=True)
    top = ranked[:num_results]

    # ── 7. Florida Sunbiz (entity registry) — part of discovery, not a separate step ──
    if enrich_sunbiz and _should_run_sunbiz(location, top) and top:
        try:
            top = enrich_leads_sunbiz_only(top)
            n_sb = sum(1 for l in top if (l.get("sunbiz_url") or "").strip())
            if n_sb:
                warnings.append(
                    f"Sunbiz: Florida registry data merged for {n_sb} of {len(top)} ranked lead(s)."
                )
            else:
                warnings.append(
                    "Sunbiz: no registry matches returned for these trade names "
                    "(names may differ from legal entity names, or lookups were blocked)."
                )
        except Exception as e:
            warnings.append(f"Sunbiz enrichment error: {e}")

    # ── 8. Store for downstream enrichment ───────────────────────────────────
    set_leads(top)

    return {
        "leads": top,
        "total": len(top),
        "breakdown": {
            "maps":            len(maps_leads),
            "yelp":            len(yelp_leads),
            "perplexity":      len(perplexity_leads),
            "reddit_mentions": reddit_total_hits,
            "merged_unique":   len(merged),
            "sunbiz_enriched": sum(1 for l in top if (l.get("sunbiz_url") or "").strip()),
        },
        "warnings": warnings,
    }
