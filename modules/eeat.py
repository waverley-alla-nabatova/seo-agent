"""
EEAT Analyzer — programmatic trust-signal detection across the site.

The LLM qualitative summary is handled by a Claude Code subagent (Phase 4).
This module produces structured signal data that subagent consumes.
"""

import json
import re
from pathlib import Path

import phonenumbers
from bs4 import BeautifulSoup, Tag


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

ADDRESS_SIGNALS_RE = re.compile(
    r"\b(\d{1,5}\s+\w+\s+(street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|place|pl|court|ct))"
    r"|\b(suite|ste|floor|fl|unit)\s+\d+"
    r"|\b[A-Z]{2}\s+\d{5}(-\d{4})?\b",  # US state + zip
    re.IGNORECASE,
)

TESTIMONIAL_CLASS_RE = re.compile(
    r"\b(testimonial|review|quote|rating|feedback|client.?say|what.?people.?say|recommendation)\b",
    re.IGNORECASE,
)

AUTHOR_RE = re.compile(
    r"\b(by\s+[A-Z][a-z]+\s+[A-Z][a-z]+|author[:\s]+[A-Z]|written\s+by|posted\s+by)\b"
)

TERMS_RE = re.compile(r"\b(terms\s+(of\s+)?(use|service|conditions)|legal)\b", re.IGNORECASE)
PRIVACY_RE = re.compile(r"\b(privacy\s+policy|data\s+policy|cookie\s+policy)\b", re.IGNORECASE)

LINKEDIN_RE = re.compile(r"linkedin\.com/(in|company)/", re.IGNORECASE)

CLIENT_LOGO_CLASS_RE = re.compile(
    r"\b(client|partner|logo|brand|customer|trusted.?by)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Generic region extractor
# ---------------------------------------------------------------------------

def get_region(soup: BeautifulSoup, region: str) -> Tag | None:
    """
    Find a page region generically.
    region: 'footer' | 'header' | 'main'
    """
    # 1. Semantic HTML tag
    tag = soup.find(region)
    if tag:
        return tag

    # 2. ARIA role
    role_map = {"footer": "contentinfo", "header": "banner", "main": "main"}
    role = role_map.get(region)
    if role:
        tag = soup.find(attrs={"role": role})
        if tag:
            return tag

    # 3. Class/id heuristic
    pattern = re.compile(rf"\b{region}\b", re.IGNORECASE)
    for el in soup.find_all(True):
        classes = " ".join(el.get("class", []))
        el_id = el.get("id", "")
        if pattern.search(classes) or pattern.search(el_id):
            return el

    # 4. Footer fallback: last significant block before </body>
    if region == "footer":
        body = soup.find("body")
        if body:
            children = [c for c in body.children if isinstance(c, Tag)]
            if children:
                return children[-1]

    return None


# ---------------------------------------------------------------------------
# Signal detectors
# ---------------------------------------------------------------------------

def detect_phone(soup: BeautifulSoup) -> dict:
    """Prefer tel: links; fallback to text validation with phonenumbers."""
    # tel: links are reliable
    for a in soup.find_all("a", href=re.compile(r"^tel:", re.IGNORECASE)):
        number_str = a["href"].replace("tel:", "").strip()
        try:
            parsed = phonenumbers.parse(number_str, None)
            if phonenumbers.is_valid_number(parsed):
                return {"present": True, "value": number_str, "source": "tel_link"}
        except Exception:
            return {"present": True, "value": number_str, "source": "tel_link"}

    # Fallback: find candidate strings, validate with phonenumbers
    text = soup.get_text(" ")
    # Look for strings that look like international phone numbers
    candidates = re.findall(r"\+?[\d\s\-\(\)\.]{10,20}", text)
    for candidate in candidates:
        clean = re.sub(r"[\s\-\(\)\.]", "", candidate)
        if len(clean) < 7:
            continue
        try:
            parsed = phonenumbers.parse(candidate, "US")
            if phonenumbers.is_valid_number(parsed):
                return {"present": True, "value": candidate.strip(), "source": "text"}
        except Exception:
            continue

    return {"present": False, "value": None, "source": None}


def detect_email(soup: BeautifulSoup) -> dict:
    # mailto: links first
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.IGNORECASE)):
        email = a["href"].replace("mailto:", "").split("?")[0].strip()
        if EMAIL_RE.match(email):
            return {"present": True, "value": email, "source": "mailto_link"}

    # Text scan
    text = soup.get_text(" ")
    match = EMAIL_RE.search(text)
    if match:
        return {"present": True, "value": match.group(), "source": "text"}

    return {"present": False, "value": None, "source": None}


def detect_address(soup: BeautifulSoup) -> dict:
    # PostalAddress schema
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    addr = item.get("address") or {}
                    if isinstance(addr, dict) and addr.get("addressLocality"):
                        return {"present": True, "source": "schema", "value": str(addr)}
        except Exception:
            pass

    # Footer/contact region text
    for region_name in ("footer", "main"):
        region = get_region(soup, region_name)
        if region and ADDRESS_SIGNALS_RE.search(region.get_text(" ")):
            return {"present": True, "source": region_name + "_text", "value": None}

    return {"present": False, "source": None, "value": None}


def detect_footer_links(soup: BeautifulSoup) -> dict:
    footer = get_region(soup, "footer")
    search_area = footer if footer else soup
    text_areas = search_area.find_all("a")
    has_terms = False
    has_privacy = False
    for a in text_areas:
        text = a.get_text(strip=True)
        href = a.get("href", "")
        combined = text + " " + href
        if TERMS_RE.search(combined):
            has_terms = True
        if PRIVACY_RE.search(combined):
            has_privacy = True
    return {"terms_link": has_terms, "privacy_link": has_privacy}


def detect_linkedin_links(soup: BeautifulSoup) -> dict:
    links = [a["href"] for a in soup.find_all("a", href=LINKEDIN_RE)]
    return {"present": bool(links), "count": len(links), "links": links[:5]}


def detect_testimonials(soup: BeautifulSoup) -> dict:
    found = []

    # blockquote elements
    for bq in soup.find_all("blockquote"):
        text = bq.get_text(strip=True)
        if len(text) > 30:
            found.append({"element": "blockquote", "snippet": text[:100]})

    # Elements with testimonial-like class/id
    for el in soup.find_all(True):
        classes = " ".join(el.get("class", []))
        el_id = el.get("id", "")
        if TESTIMONIAL_CLASS_RE.search(classes) or TESTIMONIAL_CLASS_RE.search(el_id):
            text = el.get_text(strip=True)
            if len(text) > 30:
                found.append({"element": el.name, "snippet": text[:100]})

    # Dedup by snippet prefix
    seen: set[str] = set()
    deduped = []
    for t in found:
        key = t["snippet"][:50]
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    has_attribution = False
    if deduped:
        # Check if any testimonial has name-like attribution nearby
        for bq in soup.find_all("blockquote"):
            # Look at next sibling for name
            sibling = bq.find_next_sibling()
            if sibling:
                sibling_text = sibling.get_text(strip=True)
                # Attribution pattern: "Name, Title at Company"
                if re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+", sibling_text):
                    has_attribution = True
                    break
        # Also check cite elements inside blockquote
        for cite in soup.find_all("cite"):
            if re.search(r"[A-Z][a-z]+", cite.get_text(strip=True)):
                has_attribution = True
                break

    return {
        "present": bool(deduped),
        "count": len(deduped),
        "has_attribution": has_attribution,
        "samples": deduped[:3],
    }


def detect_author_byline(soup: BeautifulSoup) -> dict:
    """Detect author byline — only meaningful on article pages."""
    # Author schema
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("author"):
                    author = item["author"]
                    name = (author.get("name") if isinstance(author, dict)
                            else author[0].get("name") if isinstance(author, list) and author
                            else None)
                    if name:
                        return {"present": True, "name": name, "source": "schema"}
        except Exception:
            pass

    # rel=author link
    author_link = soup.find("a", rel=lambda r: r and "author" in r)
    if author_link:
        return {"present": True, "name": author_link.get_text(strip=True), "source": "rel_author"}

    # Text pattern near article title
    main = get_region(soup, "main") or soup
    text = main.get_text(" ")[:2000]
    m = AUTHOR_RE.search(text)
    if m:
        return {"present": True, "name": m.group()[:60], "source": "text_pattern"}

    return {"present": False, "name": None, "source": None}


def detect_client_signals(soup: BeautifulSoup) -> dict:
    """Detect client/partner identity signals — meaningful on case study pages."""
    # Client name/logo sections
    for el in soup.find_all(True):
        classes = " ".join(el.get("class", []))
        el_id = el.get("id", "")
        if CLIENT_LOGO_CLASS_RE.search(classes) or CLIENT_LOGO_CLASS_RE.search(el_id):
            # Check for images (logos) or text (names)
            imgs = el.find_all("img")
            if imgs:
                return {"present": True, "type": "logo_section", "count": len(imgs)}

    # Check for organization name in schema
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    for field in ("client", "customer", "about", "mentions"):
                        val = item.get(field)
                        if val and isinstance(val, (str, dict)):
                            name = val if isinstance(val, str) else val.get("name", "")
                            if name:
                                return {"present": True, "type": "schema", "name": name}
        except Exception:
            pass

    return {"present": False, "type": None}


# ---------------------------------------------------------------------------
# Main analyzer — runs once per page, aggregates site-level
# ---------------------------------------------------------------------------

def analyze(crawl: list) -> dict:
    # We run the full signal scan on every page but aggregate smartly:
    # - Phone/email/address: check every page, report site-level presence
    # - Testimonials: check home + service/industry pages primarily
    # - Author bylines: only on article pages
    # - Client signals: only on case-study pages
    # - Footer links (terms/privacy): check every page

    site_signals = {
        "phone": {"present": False, "value": None, "found_on": None},
        "email": {"present": False, "value": None, "found_on": None},
        "address": {"present": False, "source": None, "found_on": None},
        "terms_link": {"present": False, "found_on": None},
        "privacy_link": {"present": False, "found_on": None},
        "linkedin_links": {"present": False, "total_count": 0, "found_on": []},
        "testimonials": {"present": False, "count": 0, "has_attribution": False, "found_on": []},
        "author_bylines": {"pages_with_byline": 0, "pages_checked": 0},
        "client_identity": {"present": False, "found_on": []},
    }

    per_page: list[dict] = []

    for result in crawl:
        if result.get("fetch_error") or result.get("status_code", 0) >= 400:
            continue
        html = load_html(result.get("raw_html_path"))
        if not html:
            continue

        url = result["url"]
        page_type = result.get("page_type", "detail:other")
        soup = BeautifulSoup(html, "lxml")

        page_record: dict = {"url": url, "page_type": page_type, "signals": {}}

        # Phone
        if not site_signals["phone"]["present"]:
            phone = detect_phone(soup)
            if phone["present"]:
                site_signals["phone"] = {**phone, "found_on": url}
            page_record["signals"]["phone"] = phone

        # Email
        if not site_signals["email"]["present"]:
            email = detect_email(soup)
            if email["present"]:
                site_signals["email"] = {**email, "found_on": url}
            page_record["signals"]["email"] = email

        # Address
        if not site_signals["address"]["present"]:
            address = detect_address(soup)
            if address["present"]:
                site_signals["address"] = {**address, "found_on": url}
            page_record["signals"]["address"] = address

        # Footer links (terms + privacy) — check every page until found
        footer_links = detect_footer_links(soup)
        if not site_signals["terms_link"]["present"] and footer_links["terms_link"]:
            site_signals["terms_link"] = {"present": True, "found_on": url}
        if not site_signals["privacy_link"]["present"] and footer_links["privacy_link"]:
            site_signals["privacy_link"] = {"present": True, "found_on": url}
        page_record["signals"]["footer_links"] = footer_links

        # LinkedIn links
        linkedin = detect_linkedin_links(soup)
        if linkedin["present"]:
            site_signals["linkedin_links"]["present"] = True
            site_signals["linkedin_links"]["total_count"] += linkedin["count"]
            if url not in site_signals["linkedin_links"]["found_on"]:
                site_signals["linkedin_links"]["found_on"].append(url)

        # Testimonials — check home and service/product/section pages
        if page_type in ("home", "section_index", "detail:service", "detail:product"):
            testimonials = detect_testimonials(soup)
            if testimonials["present"]:
                site_signals["testimonials"]["present"] = True
                site_signals["testimonials"]["count"] += testimonials["count"]
                if not site_signals["testimonials"]["has_attribution"] and testimonials["has_attribution"]:
                    site_signals["testimonials"]["has_attribution"] = True
                site_signals["testimonials"]["found_on"].append(url)
            page_record["signals"]["testimonials"] = testimonials

        # Author bylines — only on article pages
        if page_type == "detail:article":
            site_signals["author_bylines"]["pages_checked"] += 1
            byline = detect_author_byline(soup)
            if byline["present"]:
                site_signals["author_bylines"]["pages_with_byline"] += 1
            page_record["signals"]["author_byline"] = byline

        # Client identity — case study / detail:other pages with case-study-like paths
        if page_type in ("detail:other", "detail:service") or "case" in url or "success" in url:
            client = detect_client_signals(soup)
            if client["present"]:
                site_signals["client_identity"]["present"] = True
                site_signals["client_identity"]["found_on"].append(url)
            page_record["signals"]["client_identity"] = client

        per_page.append(page_record)

    # Aggregate gaps
    gaps: list[str] = []
    recommendations: list[str] = []

    if not site_signals["phone"]["present"]:
        gaps.append("no_phone_number")
        recommendations.append("Add a phone number (tel: link) to the footer and contact page")
    if not site_signals["email"]["present"]:
        gaps.append("no_email")
        recommendations.append("Add a contact email address visible on the page")
    if not site_signals["address"]["present"]:
        gaps.append("no_address")
        recommendations.append("Add a physical or mailing address to the footer or about page")
    if not site_signals["terms_link"]["present"]:
        gaps.append("no_terms_link")
        recommendations.append("Add a Terms of Use link to the footer")
    if not site_signals["privacy_link"]["present"]:
        gaps.append("no_privacy_link")
        recommendations.append("Add a Privacy Policy link to the footer")
    if not site_signals["linkedin_links"]["present"]:
        gaps.append("no_linkedin_links")
        recommendations.append("Add LinkedIn profile links for team members on the About page")
    if not site_signals["testimonials"]["present"]:
        gaps.append("no_testimonials")
        recommendations.append("Add client testimonials with full name, title, and company to key pages")
    elif not site_signals["testimonials"]["has_attribution"]:
        gaps.append("testimonials_lack_attribution")
        recommendations.append("Improve testimonials: add full name, job title, and company for each")

    byline_pages = site_signals["author_bylines"]["pages_checked"]
    byline_hits = site_signals["author_bylines"]["pages_with_byline"]
    if byline_pages > 0 and byline_hits / byline_pages < 0.5:
        gaps.append("insufficient_author_bylines")
        recommendations.append(
            f"Only {byline_hits}/{byline_pages} article pages have author bylines — add author name and date to all blog posts"
        )

    return {
        "site_signals": site_signals,
        "gaps": gaps,
        "programmatic_recommendations": recommendations,
        "per_page": per_page,
        "summary": {
            "phone_present": site_signals["phone"]["present"],
            "email_present": site_signals["email"]["present"],
            "address_present": site_signals["address"]["present"],
            "terms_link_present": site_signals["terms_link"]["present"],
            "privacy_link_present": site_signals["privacy_link"]["present"],
            "linkedin_links_present": site_signals["linkedin_links"]["present"],
            "linkedin_link_count": site_signals["linkedin_links"]["total_count"],
            "testimonials_present": site_signals["testimonials"]["present"],
            "testimonials_with_attribution": site_signals["testimonials"]["has_attribution"],
            "article_byline_coverage": (
                f"{byline_hits}/{byline_pages}"
                if byline_pages > 0 else "n/a"
            ),
            "gap_count": len(gaps),
        },
    }


def load_html(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
