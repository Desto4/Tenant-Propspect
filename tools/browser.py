"""Playwright-backed browser tools: Google Maps search, Sunbiz, website scrape, Google reviews."""
import html
import re
import threading
import requests
from datetime import datetime
from urllib.parse import quote_plus

from core.env import launch_chromium_resilient
from core.state import set_leads

_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
_USER_AGENT   = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
SCRAPE_HEADERS = {
    "User-Agent":      _USER_AGENT,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Playwright sync API is not thread-safe; serialize all Sunbiz browser work.
_SUNBIZ_PLAYWRIGHT_LOCK = threading.Lock()


def _sunbiz_unescape(text: str) -> str:
    """Decode HTML entities in scraped Sunbiz text so .title() does not produce &Amp; artifacts."""
    if not text:
        return ""
    return html.unescape(text).replace("\xa0", " ").strip()


def _sunbiz_section_lines(title: str, html_body: str) -> list:
    """Extract visible lines from a Sunbiz detailSection; title may use & or &amp; in the page."""
    for t in (title, title.replace("&", "&amp;")):
        m = re.search(
            rf"<span>\s*{re.escape(t)}\s*</span>(.*?)(?=<div[^>]*class=\"detailSection|$)",
            html_body, re.DOTALL | re.IGNORECASE,
        )
        if m:
            chunk = m.group(1)
            chunk = re.sub(r"<br\s*/?>", "\n", chunk)
            chunk = re.sub(r"<[^>]+>", "", chunk)
            chunk = _sunbiz_unescape(chunk)
            return [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    return []


def _sunbiz_officer_name(off_lines: list) -> str:
    """Pick officer/owner name from Sunbiz Officer/Director block; avoids header junk like 'Name & Address'."""
    if not off_lines:
        return ""

    def _is_header_junk(line: str) -> bool:
        u = line.lower()
        if "registered agent" in u and "address" in u:
            return True
        if re.match(r"^name\s*&\s*address\s*$", u):
            return True
        if u in ("officer/director detail", "officer director detail"):
            return True
        return False

    lines = []
    for raw in off_lines:
        ln = _sunbiz_unescape(raw)
        if not ln or _is_header_junk(ln):
            continue
        lines.append(ln)

    # Preferred: "Title …" line then ALL-CAPS style name on the next line (matches Sunbiz layout)
    for i, line in enumerate(lines):
        if line.lower().startswith("title") and i + 1 < len(lines):
            candidate = lines[i + 1]
            if re.match(r"[A-Z][A-Z0-9 ,.'\-]+$", candidate):
                return candidate.title()

    # Fallback: role keyword — name is usually on the previous line
    for i, line in enumerate(lines):
        if re.search(
            r"\b(president|owner|principal|ceo|managing member|director|founder|manager|member|treasurer|secretary|vp)\b",
            line, re.IGNORECASE,
        ):
            if i > 0:
                candidate = lines[i - 1]
                if not _is_header_junk(candidate) and len(candidate) > 2:
                    return candidate.title()
            break

    # Last resort: first non-junk line
    if lines:
        return lines[0].title()
    return ""


def search_businesses_maps(keyword: str, location: str, num_results: int = 10) -> dict:
    """Search Google Maps for businesses using a headless browser."""
    from playwright.sync_api import sync_playwright
    import time

    num_results = min(int(num_results or 10), 20)
    query       = f"{keyword} {location}"
    businesses  = []

    try:
        with sync_playwright() as pw:
            browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
            context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
            page    = context.new_page()

            page.goto(
                f"https://www.google.com/maps/search/{quote_plus(query)}",
                wait_until="domcontentloaded", timeout=30000,
            )
            time.sleep(2)

            if "consent.google.com" in page.url:
                try:
                    for sel in [
                        'button[aria-label*="Accept all"]',
                        'button[aria-label*="Agree"]',
                        'form[action*="consent"] button',
                        '.VfPpkd-LgbsSe',
                    ]:
                        btn = page.query_selector(sel)
                        if btn:
                            btn.click()
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                            time.sleep(2)
                            break
                except Exception:
                    pass

            time.sleep(2)
            fetch_count = (num_results * 2) + 5
            cards       = page.query_selector_all("a.hfpxzc")
            card_urls   = [
                c.get_attribute("href") for c in cards[:fetch_count]
                if (c.get_attribute("href") or "").startswith("https://")
            ]

            for url in card_urls:
                if len(businesses) >= num_results:
                    break
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(3)
                    html = page.content()

                    name_el = page.query_selector("h1.DUwDvf, h1.fontHeadlineLarge")
                    name    = name_el.inner_text().strip() if name_el else ""

                    addr_el = page.query_selector(
                        '[data-item-id="address"] .Io6YTe, '
                        'button[data-item-id="address"] .Io6YTe'
                    )
                    address = addr_el.inner_text().strip() if addr_el else ""

                    city, state = "", ""
                    if address:
                        parts     = address.split(",")
                        city      = parts[-2].strip() if len(parts) >= 2 else ""
                        state_zip = parts[-1].strip() if parts else ""
                        sm        = re.search(r'\b([A-Z]{2})\b', state_zip)
                        state     = sm.group(1) if sm else ""

                    phone_el = page.query_selector(
                        '[data-item-id*="phone:tel"] .Io6YTe, '
                        'button[data-tooltip="Copy phone number"] .Io6YTe'
                    )
                    phone = phone_el.inner_text().strip() if phone_el else ""

                    website_el = page.query_selector(
                        'a[data-item-id="authority"], a[aria-label*="website" i]'
                    )
                    website = ""
                    if website_el:
                        href = website_el.get_attribute("href") or ""
                        if href and not href.startswith("https://www.google"):
                            website = href.split("?")[0]

                    rating, count = "", ""
                    for el in page.query_selector_all('[aria-label]'):
                        try:
                            lbl = el.get_attribute("aria-label") or ""
                            if not rating:
                                rm = re.search(r'(\d[\.,]\d)\s*stars?', lbl, re.IGNORECASE)
                                if rm:
                                    rating = rm.group(1).replace(",", ".")
                                else:
                                    rm2 = re.search(r'^(\d)\s*stars?$', lbl.strip(), re.IGNORECASE)
                                    if rm2:
                                        rating = rm2.group(1)
                            if not count:
                                cm = re.search(r'([\d,]+)\s*reviews?', lbl, re.IGNORECASE)
                                if cm:
                                    count = cm.group(1).replace(",", "")
                            if rating and count:
                                break
                        except Exception:
                            continue
                    if not rating:
                        rm = re.search(r'(\d[\.,]\d)\s*stars?', html, re.IGNORECASE)
                        if rm:
                            rating = rm.group(1).replace(",", ".")
                    if not count:
                        cm = re.search(r'([\d,]+)\s*reviews?', html, re.IGNORECASE)
                        if cm:
                            count = cm.group(1).replace(",", "")
                    if not rating:
                        for sel in ('span.ceNzKf', 'div.F7nice > span', 'span.fontBodyMedium'):
                            try:
                                el = page.query_selector(sel)
                                if el:
                                    txt = el.inner_text().strip()
                                    rm  = re.search(r'(\d[\.,]\d)', txt)
                                    if rm:
                                        rating = rm.group(1).replace(",", ".")
                                        break
                            except Exception:
                                continue

                    if name:
                        businesses.append({
                            "trade_name": name, "entity_name": "", "formation_date": "",
                            "years_in_business": "", "sunbiz_status": "", "sunbiz_url": "",
                            "general_email": "", "business_phone": phone, "address": address,
                            "city": city, "state": state, "website": website,
                            "owner_name": "", "owner_email": "", "owner_phone": "",
                            "registered_agent": "", "reg_agent_address": "",
                            "reg_agent_email": "", "reg_agent_phone": "",
                            "instagram_url": "", "facebook_url": "",
                            "google_rating": rating, "google_review_count": count,
                            "industry": keyword, "employees": "", "linkedin_url": "",
                        })
                except Exception:
                    continue

            browser.close()
    except Exception as e:
        return {"error": str(e), "businesses": []}

    set_leads(businesses)
    return {"leads": businesses, "total": len(businesses)}


def sunbiz_lookup(business_name: str) -> dict:
    """Search Florida Sunbiz corporate registry using a headless browser."""
    from playwright.sync_api import sync_playwright
    import time

    with _SUNBIZ_PLAYWRIGHT_LOCK:
        return _sunbiz_lookup_impl(business_name)


def _sunbiz_lookup_impl(business_name: str) -> dict:
    from playwright.sync_api import sync_playwright
    import time

    try:
        with sync_playwright() as pw:
            browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
            context = browser.new_context(
                user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()
            page.goto(
                "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName",
                wait_until="domcontentloaded", timeout=30000,
            )
            time.sleep(0.5)
            page.fill("#SearchTerm", business_name)
            page.click("input[type=submit]")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(1.5)
            html = page.content()

            result_pairs = re.findall(
                r'href="(/Inquiry/CorporationSearch/SearchResultDetail[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
                html,
            )
            if not result_pairs:
                browser.close()
                return {"found": False, "searched": business_name}

            def _score(search, candidate):
                s_words = set(re.sub(r"[^a-z0-9\s]", "", search.lower()).split())
                c_words = set(re.sub(r"[^a-z0-9\s]", "", candidate.lower().replace("&amp;", "")).split())
                noise   = {"llc", "inc", "corp", "ltd", "co", "the", "a", "of", "and", "&"}
                s_core  = s_words - noise
                c_core  = c_words - noise
                if not s_core:
                    return 0
                exact    = len(s_core & c_core)
                partial  = sum(1 for sw in s_core for cw in c_core if sw in cw or cw in sw) - exact
                penalty  = abs(len(s_core) - len(c_core)) * 0.1
                return exact * 2 + partial * 0.5 - penalty

            best_href = result_pairs[0][0]
            best_score = -1
            for href, name in result_pairs:
                s = _score(business_name, name)
                if s > best_score:
                    best_score = s
                    best_href  = href

            detail_url = "https://search.sunbiz.org" + best_href.replace("&amp;", "&")
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.5)
            html = page.content()
            browser.close()

        corp_m = re.search(
            r'<div[^>]*class="[^"]*corporationName[^"]*"[^>]*>.*?<p>([^<]+)</p>\s*<p>([^<]+)</p>',
            html, re.DOTALL,
        )
        entity_type = _sunbiz_unescape(corp_m.group(1).strip()) if corp_m else ""
        entity_name = _sunbiz_unescape(corp_m.group(2).strip()) if corp_m else ""

        filing = {}
        for label, value in re.findall(
            r'<label[^>]*>\s*([^<]+?)\s*</label>\s*<span>\s*([^<]*?)\s*</span>', html
        ):
            filing[_sunbiz_unescape(label.strip())] = _sunbiz_unescape(value.strip())

        date_filed    = filing.get("Date Filed", "")
        status        = filing.get("Status", "")
        doc_number    = filing.get("Document Number", "")
        years_in_business = ""
        if date_filed:
            try:
                filed_dt = datetime.strptime(date_filed, "%m/%d/%Y")
                years_in_business = str(round((datetime.now() - filed_dt).days / 365.25, 1))
            except Exception:
                pass

        ra_lines = _sunbiz_section_lines("Registered Agent Name & Address", html)
        registered_agent  = ra_lines[0] if ra_lines else ""
        reg_agent_address = ", ".join(ra_lines[1:4]) if len(ra_lines) > 1 else ""

        off_lines = _sunbiz_section_lines("Officer/Director Detail", html)
        owner_name = _sunbiz_officer_name(off_lines)

        sunbiz_url = detail_url
        return {
            "found":             True,
            "sunbiz_url":        sunbiz_url,
            "entity_type":       entity_type,
            "entity_name":       entity_name,
            "document_number":   doc_number,
            "date_filed":        date_filed,
            "years_in_business": years_in_business,
            "sunbiz_status":     status,
            "registered_agent":  registered_agent,
            "reg_agent_address": reg_agent_address,
            "owner_name":        owner_name,
        }
    except Exception as e:
        return {"error": str(e), "searched": business_name}


def scrape_website_contact(url: str) -> dict:
    """Visit a business website and extract email, Instagram, Facebook, phone."""
    if not url:
        return {"error": "No URL provided"}
    if not url.startswith("http"):
        url = "https://" + url

    emails, instagram, facebook, phones = set(), "", "", set()
    base  = url.rstrip("/")
    pages = [base, base + "/contact", base + "/about", base + "/contact-us"]

    for page_url in pages:
        try:
            resp = requests.get(
                page_url, headers=SCRAPE_HEADERS, timeout=10, allow_redirects=True,
            )
            if resp.status_code >= 400:
                continue
            html = resp.text
            noise = {
                "example", "sentry", "wixpress", "squarespace", "wordpress",
                "schema", "domain", "email", "support@sentry", "noreply", "webmaster", "user@",
            }
            for e in re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', html):
                el = e.lower()
                if not any(n in el for n in noise) and len(el) < 80:
                    emails.add(el)
            if not instagram:
                ig = re.search(
                    r'(?:href|content)="https?://(?:www\.)?instagram\.com/([^/"?#\s]+)',
                    html, re.IGNORECASE,
                )
                if ig and ig.group(1) not in ("p", "explore", "accounts", "stories"):
                    instagram = f"https://www.instagram.com/{ig.group(1)}"
            if not facebook:
                fb = re.search(
                    r'(?:href|content)="https?://(?:www\.)?facebook\.com/([^/"?#\s]+)',
                    html, re.IGNORECASE,
                )
                if fb:
                    handle = fb.group(1)
                    if handle not in {"sharer", "share", "dialog", "plugins", "login", "groups", "events"}:
                        facebook = f"https://www.facebook.com/{handle}"
            for p in re.findall(r'href="tel:([^"]+)"', html, re.IGNORECASE):
                clean = re.sub(r"[^\d+]", "", p)
                if len(clean) >= 10:
                    phones.add(p.strip())
        except Exception:
            continue

    if not emails and not instagram and not facebook and not phones:
        try:
            from playwright.sync_api import sync_playwright
            import time as _time
            with sync_playwright() as pw:
                browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
                ctx  = browser.new_context(user_agent=_USER_AGENT)
                pg   = ctx.new_page()
                noise = {
                    "example", "sentry", "wixpress", "squarespace", "wordpress",
                    "schema", "domain", "email", "support@sentry", "noreply", "webmaster", "user@",
                }
                for page_url in pages[:2]:
                    try:
                        pg.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                        _time.sleep(1)
                        html = pg.content()
                        for e in re.findall(
                            r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', html
                        ):
                            el = e.lower()
                            if not any(n in el for n in noise) and len(el) < 80:
                                emails.add(el)
                        if not instagram:
                            ig = re.search(
                                r'(?:href|content)="https?://(?:www\.)?instagram\.com/([^/"?#\s]+)',
                                html, re.IGNORECASE,
                            )
                            if ig and ig.group(1) not in ("p", "explore", "accounts", "stories"):
                                instagram = f"https://www.instagram.com/{ig.group(1)}"
                        if not facebook:
                            fb = re.search(
                                r'(?:href|content)="https?://(?:www\.)?facebook\.com/([^/"?#\s]+)',
                                html, re.IGNORECASE,
                            )
                            if fb:
                                handle = fb.group(1)
                                if handle not in {"sharer", "share", "dialog", "plugins", "login", "groups", "events"}:
                                    facebook = f"https://www.facebook.com/{handle}"
                        for p in re.findall(r'href="tel:([^"]+)"', html, re.IGNORECASE):
                            clean = re.sub(r"[^\d+]", "", p)
                            if len(clean) >= 10:
                                phones.add(p.strip())
                        if emails or instagram or facebook or phones:
                            break
                    except Exception:
                        continue
                browser.close()
        except Exception:
            pass

    priority_prefixes = ("info", "contact", "hello", "office", "admin", "mail", "booking")
    general_email = ""
    for e in emails:
        if any(e.startswith(p + "@") for p in priority_prefixes):
            general_email = e
            break
    if not general_email and emails:
        general_email = sorted(emails)[0]

    return {
        "general_email": general_email,
        "all_emails":    sorted(emails)[:6],
        "instagram_url": instagram,
        "facebook_url":  facebook,
        "phones":        list(phones)[:3],
    }


def get_google_reviews(business_name: str, city: str = "", state: str = "") -> dict:
    """Fetch Google rating and review count via headless browser."""
    from playwright.sync_api import sync_playwright
    import time

    query  = f"{business_name} {city} {state}".strip()
    rating, count, html = "", "", ""

    try:
        with sync_playwright() as pw:
            browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
            context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
            page    = context.new_page()
            page.goto(
                f"https://www.google.com/maps/search/{quote_plus(query)}",
                wait_until="domcontentloaded", timeout=20000,
            )
            time.sleep(2)
            first = page.query_selector("a.hfpxzc")
            if first:
                first.click()
                time.sleep(4)
            html = page.content()

            for el in page.query_selector_all('[aria-label]'):
                try:
                    lbl = el.get_attribute("aria-label") or ""
                    if not rating:
                        rm = re.search(r'(\d[\.,]\d)\s*stars?', lbl, re.IGNORECASE)
                        if rm:
                            rating = rm.group(1).replace(",", ".")
                        else:
                            rm2 = re.search(r'^(\d)\s*stars?$', lbl.strip(), re.IGNORECASE)
                            if rm2:
                                rating = rm2.group(1)
                    if not count:
                        cm = re.search(r'([\d,]+)\s*reviews?', lbl, re.IGNORECASE)
                        if cm:
                            count = cm.group(1).replace(",", "")
                    if rating and count:
                        break
                except Exception:
                    continue

            browser.close()
    except Exception:
        pass

    if not rating and html:
        rm = re.search(r'(\d[\.,]\d)\s*stars?', html, re.IGNORECASE)
        if rm:
            rating = rm.group(1).replace(",", ".")
    if not count and html:
        cm = re.search(r'([\d,]+)\s*reviews?', html, re.IGNORECASE)
        if cm:
            count = cm.group(1).replace(",", "")

    return {"google_rating": rating, "google_review_count": count}


# ── Yelp ──────────────────────────────────────────────────────────────────────

def search_businesses_yelp(keyword: str, location: str, num_results: int = 10) -> dict:
    """
    Search Yelp for businesses by keyword and location using a headless browser.
    Returns leads with name, address, phone, website, Yelp rating, review count,
    and Yelp URL — same lead shape as search_businesses_maps.
    """
    from playwright.sync_api import sync_playwright
    import time

    num_results = min(int(num_results or 10), 20)
    businesses  = []

    try:
        with sync_playwright() as pw:
            browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
            context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
            page    = context.new_page()

            search_url = (
                f"https://www.yelp.com/search"
                f"?find_desc={quote_plus(keyword)}&find_loc={quote_plus(location)}"
            )
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            # Give Yelp time to pass bot checks and fully render the list
            time.sleep(5)
            # Scroll to trigger lazy-loaded results
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(2)

            # Collect detail-page URLs from the results list
            links = page.query_selector_all('a[href*="/biz/"]')
            biz_urls = []
            seen = set()
            for a in links:
                href = a.get_attribute("href") or ""
                # Skip ads, photos, reviews — only business pages
                if "/biz/" not in href:
                    continue
                # Normalise to absolute
                if href.startswith("/"):
                    href = "https://www.yelp.com" + href
                # Strip query params and fragments
                href = href.split("?")[0].split("#")[0]
                if href not in seen:
                    seen.add(href)
                    biz_urls.append(href)
                if len(biz_urls) >= num_results * 2:
                    break

            for biz_url in biz_urls:
                if len(businesses) >= num_results:
                    break
                try:
                    page.goto(biz_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(2)
                    html = page.content()

                    # ── Name ──
                    name_el = page.query_selector('h1')
                    name    = name_el.inner_text().strip() if name_el else ""
                    if not name:
                        continue

                    # ── Rating ──
                    rating = ""
                    rating_el = page.query_selector('[aria-label*="star rating"]')
                    if rating_el:
                        lbl = rating_el.get_attribute("aria-label") or ""
                        rm  = re.search(r'(\d[\.,]\d)', lbl)
                        if rm:
                            rating = rm.group(1).replace(",", ".")
                    if not rating:
                        rm = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', html)
                        if rm:
                            rating = rm.group(1)

                    # ── Review count ──
                    review_count = ""
                    rc_el = page.query_selector('[aria-label*="review"]')
                    if rc_el:
                        lbl = rc_el.get_attribute("aria-label") or rc_el.inner_text() or ""
                        cm  = re.search(r'([\d,]+)', lbl)
                        if cm:
                            review_count = cm.group(1).replace(",", "")
                    if not review_count:
                        rm = re.search(r'"reviewCount"\s*:\s*(\d+)', html)
                        if rm:
                            review_count = rm.group(1)

                    # ── Address ──
                    address = ""
                    addr_el = page.query_selector('address')
                    if addr_el:
                        address = " ".join(addr_el.inner_text().split())
                    if not address:
                        rm = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html)
                        if rm:
                            address = rm.group(1)

                    city, state = "", ""
                    city_m  = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html)
                    state_m = re.search(r'"addressRegion"\s*:\s*"([^"]+)"',   html)
                    if city_m:  city  = city_m.group(1)
                    if state_m: state = state_m.group(1)

                    # ── Phone ──
                    phone = ""
                    phone_m = re.search(r'"telephone"\s*:\s*"([^"]+)"', html)
                    if phone_m:
                        phone = phone_m.group(1)
                    if not phone:
                        tel_el = page.query_selector('a[href^="tel:"]')
                        if tel_el:
                            phone = (tel_el.get_attribute("href") or "").replace("tel:", "").strip()

                    # ── Website ──
                    website = ""
                    ws_el   = page.query_selector('a[href*="biz_redir"]')
                    if ws_el:
                        raw = ws_el.get_attribute("href") or ""
                        # Yelp wraps external links — extract the url= param
                        url_m = re.search(r'[?&]url=([^&]+)', raw)
                        if url_m:
                            from urllib.parse import unquote
                            website = unquote(url_m.group(1)).split("?")[0]

                    businesses.append({
                        "trade_name":          name,
                        "entity_name":         "",
                        "formation_date":      "",
                        "years_in_business":   "",
                        "sunbiz_status":       "",
                        "sunbiz_url":          "",
                        "general_email":       "",
                        "business_phone":      phone,
                        "address":             address,
                        "city":                city,
                        "state":               state,
                        "website":             website,
                        "owner_name":          "",
                        "owner_email":         "",
                        "owner_phone":         "",
                        "registered_agent":    "",
                        "reg_agent_address":   "",
                        "reg_agent_email":     "",
                        "reg_agent_phone":     "",
                        "instagram_url":       "",
                        "facebook_url":        "",
                        "google_rating":       "",
                        "google_review_count": "",
                        "yelp_rating":         rating,
                        "yelp_review_count":   review_count,
                        "yelp_url":            biz_url,
                        "industry":            keyword,
                        "employees":           "",
                        "linkedin_url":        "",
                        "source":              "yelp",
                    })

                except Exception:
                    continue

            browser.close()
    except Exception as e:
        return {"error": str(e), "businesses": []}

    if not businesses:
        return {
            "leads":   [],
            "total":   0,
            "warning": (
                "Yelp returned no results. Yelp uses aggressive bot detection that may block "
                "cloud/server IPs. This tool works best when running the app locally. "
                "Try Google Maps search (search_businesses_maps) as an alternative."
            ),
        }
    set_leads(businesses)
    return {"leads": businesses, "total": len(businesses)}


# ── Reddit ─────────────────────────────────────────────────────────────────────

def search_reddit(query: str, subreddits: list = None, num_results: int = 10) -> dict:
    """
    Search Reddit for posts/discussions matching a query.
    Uses Reddit's JSON API (no browser required for most queries, but falls back
    to headless Playwright for JS-gated pages).

    Useful for:
    - Finding businesses mentioned in local subreddits (e.g. r/miami)
    - Spotting businesses discussing expansion or new locations
    - Gathering tenant prospect intelligence from community recommendations

    Returns a list of posts: {title, url, subreddit, score, comments, snippet, author}.
    """
    subreddits = subreddits or []
    results    = []

    def _parse_posts(data: dict) -> list:
        posts = []
        for child in (data.get("data", {}).get("children") or []):
            post = child.get("data", {})
            if not post:
                continue
            posts.append({
                "title":     post.get("title", ""),
                "url":       "https://www.reddit.com" + post.get("permalink", ""),
                "subreddit": post.get("subreddit_name_prefixed", ""),
                "score":     post.get("score", 0),
                "comments":  post.get("num_comments", 0),
                "snippet":   (post.get("selftext") or "")[:300].strip(),
                "author":    post.get("author", ""),
                "created":   post.get("created_utc", 0),
            })
        return posts

    headers = {
        "User-Agent": "MMGAgent/1.0 (lead research tool)",
        "Accept":     "application/json",
    }

    # ── Strategy 1: subreddit-scoped search ──────────────────────────────────
    if subreddits:
        for sr in subreddits:
            if len(results) >= num_results:
                break
            sr = sr.lstrip("r/")
            try:
                url  = f"https://www.reddit.com/r/{sr}/search.json"
                resp = requests.get(
                    url,
                    params={"q": query, "restrict_sr": "1", "sort": "relevance", "limit": num_results},
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 200:
                    results.extend(_parse_posts(resp.json()))
            except Exception:
                continue

    # ── Strategy 2: site-wide JSON search ────────────────────────────────────
    if len(results) < num_results:
        for base in ["https://www.reddit.com", "https://old.reddit.com"]:
            try:
                resp = requests.get(
                    f"{base}/search.json",
                    params={"q": query, "sort": "relevance", "limit": num_results},
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 200:
                    results.extend(_parse_posts(resp.json()))
                    break
            except Exception:
                continue

    # ── Strategy 3: Playwright fallback (handles 403 / JS-gated) ─────────────
    if not results:
        try:
            from playwright.sync_api import sync_playwright
            import time
            with sync_playwright() as pw:
                browser = launch_chromium_resilient(pw, headless=True, args=_BROWSER_ARGS)
                context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
                page    = context.new_page()

                # Try old Reddit first — simpler HTML, easier to scrape
                for search_url in [
                    f"https://old.reddit.com/search?q={quote_plus(query)}&sort=relevance",
                    f"https://www.reddit.com/search/?q={quote_plus(query)}&sort=relevance",
                ]:
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(3)
                        html = page.content()

                        # old.reddit.com structure
                        old_posts = re.findall(
                            r'<p class="title"><a[^>]+href="(/r/[^"]+/comments/[^"]+)"[^>]*>([^<]+)</a>',
                            html,
                        )
                        if old_posts:
                            for link, title in old_posts[:num_results]:
                                results.append({
                                    "title":     title.strip(),
                                    "url":       "https://www.reddit.com" + link.split("?")[0],
                                    "subreddit": "",
                                    "score":     0,
                                    "comments":  0,
                                    "snippet":   "",
                                    "author":    "",
                                    "created":   0,
                                })
                            break

                        # new reddit fallback — pick h3 headings paired with /comments/ links
                        links  = re.findall(r'href="(/r/[^"]+/comments/[^"?]+)', html)
                        titles = re.findall(r'<h3[^>]*>([^<]{10,200})</h3>', html)
                        for i, (link, title) in enumerate(zip(links, titles)):
                            if i >= num_results:
                                break
                            results.append({
                                "title":     title.strip(),
                                "url":       "https://www.reddit.com" + link,
                                "subreddit": "",
                                "score":     0,
                                "comments":  0,
                                "snippet":   "",
                                "author":    "",
                                "created":   0,
                            })
                        if results:
                            break
                    except Exception:
                        continue

                browser.close()
        except Exception:
            pass

    # Deduplicate by URL and cap
    seen, deduped = set(), []
    for post in results:
        url = post.get("url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(post)

    final = deduped[:num_results]
    if not final:
        return {
            "posts":   [],
            "total":   0,
            "query":   query,
            "warning": (
                "Reddit returned no results. This usually means Reddit is rate-limiting "
                "or blocking requests from this IP. Try again in a few minutes, or run "
                "the app locally where Reddit access is not restricted."
            ),
        }
    return {"posts": final, "total": len(final), "query": query}
