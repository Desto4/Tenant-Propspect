"""
Playwright-based scrapers for Google Maps, Sunbiz, and business websites.

Classes
-------
PlaywrightScraper   -- base: browser launch + consent-page handling
MapsScraper         -- search Google Maps listings
SunbizScraper       -- look up Florida corporate registry

Module-level functions are thin wrappers so the rest of the codebase
can call them without instantiating a class every time.
"""

import re
import time
import requests
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# Shared HTTP headers (used by requests-based scraping)
# ---------------------------------------------------------------------------

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class PlaywrightScraper:
    """Shared browser-launch and consent-page logic for all Playwright scrapers."""

    _UA = SCRAPE_HEADERS["User-Agent"]
    _VIEWPORT = {"width": 1280, "height": 800}
    _LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

    def _new_page(self, pw):
        """Launch a headless Chromium browser and return (browser, page)."""
        browser = pw.chromium.launch(headless=True, args=self._LAUNCH_ARGS)
        ctx = browser.new_context(user_agent=self._UA, viewport=self._VIEWPORT)
        return browser, ctx.new_page()

    def _accept_consent(self, page):
        """Click through Google's cookie-consent screen if present."""
        if "consent.google.com" not in page.url:
            return
        for sel in [
            'button[aria-label*="Accept all"]',
            'button[aria-label*="Agree"]',
            'form[action*="consent"] button',
            '.VfPpkd-LgbsSe',
        ]:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                time.sleep(2)
                break


# ---------------------------------------------------------------------------
# Google Maps scraper
# ---------------------------------------------------------------------------

class MapsScraper(PlaywrightScraper):
    """Scrape Google Maps to discover and return business listings."""

    def search(self, keyword, location, num_results=10):
        from playwright.sync_api import sync_playwright

        num_results = min(int(num_results or 10), 20)
        businesses = []

        try:
            with sync_playwright() as pw:
                browser, page = self._new_page(pw)
                page.goto(
                    f"https://www.google.com/maps/search/{quote_plus(keyword + ' ' + location)}",
                    wait_until="domcontentloaded", timeout=30000,
                )
                time.sleep(2)
                self._accept_consent(page)
                time.sleep(2)

                fetch_count = (num_results * 2) + 5
                card_urls = [
                    c.get_attribute("href")
                    for c in page.query_selector_all("a.hfpxzc")[:fetch_count]
                    if (c.get_attribute("href") or "").startswith("https://")
                ]

                for url in card_urls:
                    if len(businesses) >= num_results:
                        break
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(3)
                        biz = self._parse_listing(page, keyword)
                        if biz:
                            businesses.append(biz)
                    except Exception:
                        continue

                browser.close()

        except Exception as e:
            return {"error": str(e), "businesses": []}

        return {"leads": businesses, "total": len(businesses)}

    # -- Helpers ----------------------------------------------------------------

    def _parse_listing(self, page, keyword):
        html = page.content()

        name_el = page.query_selector("h1.DUwDvf, h1.fontHeadlineLarge")
        name = name_el.inner_text().strip() if name_el else ""
        if not name:
            return None

        addr_el = page.query_selector(
            '[data-item-id="address"] .Io6YTe, button[data-item-id="address"] .Io6YTe'
        )
        address = addr_el.inner_text().strip() if addr_el else ""

        city, state = "", ""
        if address:
            parts = address.split(",")
            if len(parts) >= 2:
                city = parts[-2].strip()
            m = re.search(r'\b([A-Z]{2})\b', parts[-1] if parts else "")
            state = m.group(1) if m else ""

        phone_el = page.query_selector(
            '[data-item-id*="phone:tel"] .Io6YTe, '
            'button[data-tooltip="Copy phone number"] .Io6YTe'
        )
        phone = phone_el.inner_text().strip() if phone_el else ""

        website = ""
        website_el = page.query_selector(
            'a[data-item-id="authority"], a[aria-label*="website" i]'
        )
        if website_el:
            href = website_el.get_attribute("href") or ""
            if href and not href.startswith("https://www.google"):
                website = href.split("?")[0]

        rating, count = self._extract_rating(page, html)

        return {
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
        }

    def _extract_rating(self, page, html):
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
                        rm = re.search(r'(\d[\.,]\d)', el.inner_text().strip())
                        if rm:
                            rating = rm.group(1).replace(",", ".")
                            break
                except Exception:
                    continue

        return rating, count

    def get_reviews(self, business_name, city="", state=""):
        """Open Maps, click the first result, return rating + review count."""
        from playwright.sync_api import sync_playwright

        query = f"{business_name} {city} {state}".strip()
        rating, count = "", ""

        try:
            with sync_playwright() as pw:
                browser, page = self._new_page(pw)
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
                rating, count = self._extract_rating(page, html)
                browser.close()
        except Exception:
            pass

        return {"google_rating": rating, "google_review_count": count}


# ---------------------------------------------------------------------------
# Sunbiz scraper
# ---------------------------------------------------------------------------

class SunbizScraper(PlaywrightScraper):
    """Look up a business in the Florida Sunbiz corporate registry."""

    def lookup(self, business_name):
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser, page = self._new_page(pw)
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
                    r'href="(/Inquiry/CorporationSearch/SearchResultDetail[^"]+)"'
                    r'[^>]*>\s*([^<]+?)\s*</a>',
                    html,
                )
                if not result_pairs:
                    browser.close()
                    return {"found": False, "searched": business_name}

                best_href = self._best_match(business_name, result_pairs)
                detail_url = "https://search.sunbiz.org" + best_href.replace("&amp;", "&")
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(1.5)
                detail_html = page.content()
                browser.close()

            return self._parse_detail(detail_html, detail_url)

        except Exception as e:
            return {"error": str(e), "searched": business_name}

    # -- Helpers ----------------------------------------------------------------

    def _best_match(self, search, pairs):
        noise = {"llc", "inc", "corp", "ltd", "co", "the", "a", "of", "and", "&"}

        def score(candidate):
            s = set(re.sub(r"[^a-z0-9\s]", "", search.lower()).split()) - noise
            c = set(re.sub(r"[^a-z0-9\s]", "", candidate.lower().replace("&amp;", "")).split()) - noise
            if not s:
                return 0
            exact = len(s & c)
            partial = sum(1 for sw in s for cw in c if sw in cw or cw in sw) - exact
            return exact * 2 + partial * 0.5 - abs(len(s) - len(c)) * 0.1

        best_href, best_score = pairs[0][0], -1
        for href, name in pairs:
            s = score(name)
            if s > best_score:
                best_score, best_href = s, href
        return best_href

    def _parse_detail(self, html, detail_url):
        from datetime import datetime

        corp_m = re.search(
            r'<div[^>]*class="[^"]*corporationName[^"]*"[^>]*>.*?<p>([^<]+)</p>\s*<p>([^<]+)</p>',
            html, re.DOTALL,
        )
        entity_type = corp_m.group(1).strip() if corp_m else ""
        entity_name = corp_m.group(2).strip().replace("&amp;", "&") if corp_m else ""

        filing = {
            label.strip(): value.strip()
            for label, value in re.findall(
                r'<label[^>]*>\s*([^<]+?)\s*</label>\s*<span>\s*([^<]*?)\s*</span>', html
            )
        }
        date_filed  = filing.get("Date Filed", "")
        status      = filing.get("Status", "")
        doc_number  = filing.get("Document Number", "")

        years_in_business = ""
        if date_filed:
            try:
                filed_dt = datetime.strptime(date_filed, "%m/%d/%Y")
                years_in_business = str(round((datetime.now() - filed_dt).days / 365.25, 1))
            except Exception:
                pass

        def section_text(title):
            m = re.search(
                rf"<span>\s*{re.escape(title)}\s*</span>"
                r"(.*?)(?=<div[^>]*class=\"detailSection|$)",
                html, re.DOTALL | re.IGNORECASE,
            )
            if not m:
                return []
            chunk = re.sub(r"<br\s*/?>", "\n", m.group(1))
            chunk = re.sub(r"<[^>]+>", "", chunk)
            chunk = chunk.replace("&amp;", "&").replace("&nbsp;", " ")
            return [ln.strip() for ln in chunk.splitlines() if ln.strip()]

        ra_lines = section_text("Registered Agent Name &amp; Address")
        pa_lines = section_text("Principal Address")

        owner_name = ""
        off_m = re.search(
            r"<span>\s*Officer/Director Detail\s*</span>"
            r"(.*?)(?=<div[^>]*class=\"detailSection|$)",
            html, re.DOTALL | re.IGNORECASE,
        )
        if off_m:
            chunk = re.sub(r"<br\s*/?>", "\n", off_m.group(1))
            chunk = re.sub(r"<[^>]+>", "\n", chunk)
            chunk = chunk.replace("&amp;", "&").replace("&nbsp;", " ")
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            for i, line in enumerate(lines):
                if line.lower().startswith("title") and i + 1 < len(lines):
                    candidate = lines[i + 1]
                    if re.match(r"[A-Z][A-Z ,.\-']+$", candidate):
                        owner_name = candidate
                        break

        return {
            "found":             True,
            "sunbiz_url":        detail_url,
            "entity_type":       entity_type,
            "entity_name":       entity_name,
            "document_number":   doc_number,
            "date_filed":        date_filed,
            "years_in_business": years_in_business,
            "sunbiz_status":     status,
            "principal_address": ", ".join(pa_lines),
            "registered_agent":  ra_lines[0] if ra_lines else "",
            "reg_agent_address": ", ".join(ra_lines[1:]) if len(ra_lines) > 1 else "",
            "owner_name":        owner_name,
        }


# ---------------------------------------------------------------------------
# Module-level singleton instances + wrapper functions
# ---------------------------------------------------------------------------

_maps_scraper  = MapsScraper()
_sunbiz_scraper = SunbizScraper()


def search_businesses_maps(keyword, location, num_results=10):
    return _maps_scraper.search(keyword, location, num_results)


def sunbiz_lookup(business_name):
    return _sunbiz_scraper.lookup(business_name)


def get_google_reviews(business_name, city="", state=""):
    return _maps_scraper.get_reviews(business_name, city, state)


def scrape_website_contact(url):
    """
    Extract email, Instagram, Facebook, and phone from a business website.
    Uses requests first; falls back to Playwright for JS-heavy sites.
    """
    if not url:
        return {"error": "No URL provided"}
    if not url.startswith("http"):
        url = "https://" + url

    emails, phones = set(), set()
    instagram, facebook = "", ""

    base = url.rstrip("/")
    pages = [base, base + "/contact", base + "/about", base + "/contact-us"]

    def _extract_from_html(html):
        nonlocal instagram, facebook
        noise = {
            "example", "sentry", "wixpress", "squarespace", "wordpress",
            "schema", "domain", "email", "support@sentry", "noreply",
            "webmaster", "user@",
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

    for page_url in pages:
        try:
            resp = requests.get(page_url, headers=SCRAPE_HEADERS, timeout=10, allow_redirects=True)
            if resp.status_code < 400:
                _extract_from_html(resp.text)
        except Exception:
            continue

    # Playwright fallback for JS-heavy sites
    if not emails and not instagram and not facebook and not phones:
        try:
            from playwright.sync_api import sync_playwright
            _fallback = PlaywrightScraper()
            with sync_playwright() as pw:
                b, pg = _fallback._new_page(pw)
                for page_url in pages[:2]:
                    try:
                        pg.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                        time.sleep(1)
                        _extract_from_html(pg.content())
                        if emails or instagram or facebook or phones:
                            break
                    except Exception:
                        continue
                b.close()
        except Exception:
            pass

    priority = ("info", "contact", "hello", "office", "admin", "mail", "booking")
    general_email = next(
        (e for e in emails if any(e.startswith(p + "@") for p in priority)),
        sorted(emails)[0] if emails else "",
    )

    return {
        "general_email": general_email,
        "all_emails":    sorted(emails)[:6],
        "instagram_url": instagram,
        "facebook_url":  facebook,
        "phones":        list(phones)[:3],
    }
