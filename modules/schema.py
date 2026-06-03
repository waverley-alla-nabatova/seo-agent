"""
Schema Analyzer — JSON-LD extraction, validation, missing-schema detection.

Produces:
- per_page: what schemas are present, what's missing, field-level validation errors
- generation_requests: batched by page type for the Content Rewriter subagent
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Expected schema types per page type
# ---------------------------------------------------------------------------

EXPECTED_SCHEMAS: dict[str, list[str]] = {
    "home":            ["Organization", "WebSite"],
    "section_index":   ["BreadcrumbList"],
    "detail:article":  ["Article", "BreadcrumbList"],
    "detail:job":      ["JobPosting", "BreadcrumbList"],
    "detail:service":  ["Service", "BreadcrumbList"],
    "detail:product":  ["Product", "BreadcrumbList"],
    "detail:other":    ["BreadcrumbList"],
}

# Required fields per schema @type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "Organization":   ["name", "url", "logo"],
    "WebSite":        ["name", "url"],
    "Service":        ["name", "url", "provider"],
    "Product":        ["name"],
    "Article":        ["headline", "author", "datePublished"],
    "BlogPosting":    ["headline", "author", "datePublished"],
    "JobPosting":     ["title", "description", "datePosted", "hiringOrganization"],
    "BreadcrumbList": ["itemListElement"],
    "FAQPage":        ["mainEntity"],
}

# Headings that indicate a FAQ section is present on the page
FAQ_HEADING_RE = re.compile(
    r"\b(faq|frequently\s+asked|common\s+questions?|questions?\s+&\s+answers?|q\s*[&+]\s*a)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_html(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def extract_jsonld_blocks(html: str) -> list[dict]:
    """Extract all JSON-LD blocks from raw HTML. Returns parsed dicts."""
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            # Handle @graph arrays
            if isinstance(data, dict) and "@graph" in data:
                for item in data["@graph"]:
                    if isinstance(item, dict):
                        blocks.append(item)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        blocks.append(item)
            elif isinstance(data, dict):
                blocks.append(data)
        except json.JSONDecodeError:
            pass
    return blocks


def normalize_type(schema_type) -> list[str]:
    """Normalize @type to a list of strings."""
    if isinstance(schema_type, str):
        return [schema_type]
    if isinstance(schema_type, list):
        return [t for t in schema_type if isinstance(t, str)]
    return []


def has_faq_section(html: str) -> bool:
    """True if the page has a visible FAQ heading."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if FAQ_HEADING_RE.search(tag.get_text(strip=True)):
            return True
    # Also check elements with FAQ-like class or id
    for tag in soup.find_all(attrs={"class": FAQ_HEADING_RE}):
        return True
    return False


def validate_fields(block: dict, schema_type: str) -> list[str]:
    """Return list of missing required fields for a given @type."""
    required = REQUIRED_FIELDS.get(schema_type, [])
    return [f for f in required if not block.get(f)]


def page_body_snippet(html: str, chars: int = 500) -> str:
    """Extract a short visible-text snippet for the Content Rewriter."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)[:chars]


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

def analyze(crawl: list) -> dict:
    per_page: list[dict] = []
    generation_requests: list[dict] = []  # grouped later by page type

    for result in crawl:
        if result.get("fetch_error") or result.get("status_code", 0) >= 400:
            continue
        html = load_html(result.get("raw_html_path"))
        if not html:
            continue

        url = result["url"]
        page_type = result.get("page_type", "detail:other")

        # Extract present schemas
        blocks = extract_jsonld_blocks(html)
        present_types: list[str] = []
        field_errors: list[dict] = []

        for block in blocks:
            types = normalize_type(block.get("@type", ""))
            for t in types:
                present_types.append(t)
                missing_fields = validate_fields(block, t)
                if missing_fields:
                    field_errors.append({
                        "type": t,
                        "missing_fields": missing_fields,
                    })

        # Determine expected types for this page
        expected = list(EXPECTED_SCHEMAS.get(page_type, ["BreadcrumbList"]))

        # Check for FAQ section — adds FAQPage to expected if present
        faq_present = has_faq_section(html)
        if faq_present and "FAQPage" not in expected:
            expected.append("FAQPage")

        # Determine missing types
        # Treat Article/BlogPosting as interchangeable
        article_aliases = {"Article", "BlogPosting", "NewsArticle", "TechArticle"}
        present_set = set(present_types)

        def is_satisfied(expected_type: str) -> bool:
            if expected_type == "Article":
                return bool(present_set & article_aliases)
            return expected_type in present_set

        missing_types = [t for t in expected if not is_satisfied(t)]

        record = {
            "url": url,
            "page_type": page_type,
            "present_types": present_types,
            "expected_types": expected,
            "missing_types": missing_types,
            "field_errors": field_errors,
            "faq_section_present": faq_present,
            "schema_block_count": len(blocks),
        }
        per_page.append(record)

        # Package generation requests for missing schemas
        if missing_types:
            # Get page context for the LLM
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            meta_desc = soup.find("meta", attrs={"name": lambda n: n and n.lower() == "description"})
            h1_tag = soup.find("h1")

            generation_requests.append({
                "url": url,
                "page_type": page_type,
                "missing_types": missing_types,
                "context": {
                    "title": title_tag.get_text(strip=True) if title_tag else None,
                    "description": meta_desc.get("content", "").strip() if meta_desc else None,
                    "h1": h1_tag.get_text(strip=True) if h1_tag else None,
                    "body_snippet": page_body_snippet(html),
                    "faq_section_present": faq_present,
                },
            })

    # Summary
    all_missing = [t for p in per_page for t in p["missing_types"]]
    missing_by_type: dict[str, int] = {}
    for t in all_missing:
        missing_by_type[t] = missing_by_type.get(t, 0) + 1

    all_errors = [e for p in per_page for e in p["field_errors"]]
    errors_by_type: dict[str, int] = {}
    for e in all_errors:
        errors_by_type[e["type"]] = errors_by_type.get(e["type"], 0) + 1

    return {
        "per_page": per_page,
        "generation_requests": generation_requests,
        "summary": {
            "pages_analyzed": len(per_page),
            "pages_with_any_schema": sum(1 for p in per_page if p["present_types"]),
            "pages_with_missing_schema": sum(1 for p in per_page if p["missing_types"]),
            "pages_with_field_errors": sum(1 for p in per_page if p["field_errors"]),
            "pages_with_faq_section": sum(1 for p in per_page if p["faq_section_present"]),
            "missing_by_type": missing_by_type,
            "field_errors_by_type": errors_by_type,
            "generation_requests_count": len(generation_requests),
        },
    }
