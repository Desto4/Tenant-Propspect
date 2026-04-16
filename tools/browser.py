"""Playwright-backed browser tools: Google Maps search, Sunbiz, website scrape, Google reviews."""
import re
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
        entity_type = corp_m.group(1).strip() if corp_m else ""
        entity_name = corp_m.group(2).strip().replace("&amp;", "&") if corp_m else ""

        filing = {}
        for label, value in re.findall(
            r'<label[^>]*>\s*([^<]+?)\s*</label>\s*<span>\s*([^<]*?)\s*</span>', html
        ):
            filing[label.strip()] = value.strip()

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

        def _section_text(title, html_body):
            m = re.search(
                rf"<span>\s*{re.escape(title)}\s*</span>(.*?)(?=<div[^>]*class=\"detailSection|$)",
                html_body, re.DOTALL | re.IGNORECASE,
            )
            if not m:
                return []
            chunk = re.sub(r"<br\s*/?>", "\n", m.group(1))
            chunk = re.sub(r"<[^>]+>", "", chunk)
            return [ln.strip() for ln in chunk.splitlines() if ln.strip()]

        ra_lines = _section_text("Registered Agent Name & Address", html)
        registered_agent  = ra_lines[0] if ra_lines else ""
        reg_agent_address = ", ".join(ra_lines[1:4]) if len(ra_lines) > 1 else ""

        off_lines = _section_text("Officer/Director Detail", html)
        owner_name = ""
        for i, line in enumerate(off_lines):
            if re.search(r'\b(president|owner|principal|ceo|managing member|director|founder)\b', line, re.IGNORECASE):
                if i > 0:
                    owner_name = off_lines[i - 1].title()
                break
        if not owner_name and off_lines:
            owner_name = off_lines[0].title()

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
