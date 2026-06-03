"""
Metadata Analyzer — titles, meta descriptions, headings, OG/Twitter tags, duplicates.

Flags pages that need rewrites and packages them as rewrite_requests
for the Content Rewriter subagent.
"""

from difflib import SequenceMatcher
from pathlib import Path

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

TITLE_MIN = 30
TITLE_MAX = 60
DESC_MIN = 120
DESC_MAX = 158
H1_TITLE_SIMILARITY_MIN = 0.35   # below this → flag misalignment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_html(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def get_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def get_title(soup: BeautifulSoup) -> str | None:
    tag = soup.find("title")
    return tag.get_text(strip=True) if tag else None


def get_meta_description(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": lambda n: n and n.lower() == "description"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def get_h1s(soup: BeautifulSoup) -> list[str]:
    return [h.get_text(strip=True) for h in soup.find_all("h1")]


def get_headings(soup: BeautifulSoup) -> list[tuple[int, str]]:
    """Return list of (level, text) for all h1–h6 tags in document order."""
    return [
        (int(h.name[1]), h.get_text(strip=True))
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    ]


def get_og_tags(soup: BeautifulSoup) -> dict[str, str]:
    tags = {}
    for meta in soup.find_all("meta", property=lambda p: p and p.startswith("og:")):
        prop = meta.get("property", "").strip()
        content = meta.get("content", "").strip()
        if prop and content:
            tags[prop] = content
    return tags


def get_twitter_tags(soup: BeautifulSoup) -> dict[str, str]:
    tags = {}
    for meta in soup.find_all("meta", attrs={"name": lambda n: n and n.lower().startswith("twitter:")}):
        name = meta.get("name", "").strip().lower()
        content = meta.get("content", "").strip()
        if name and content:
            tags[name] = content
    return tags


def title_similarity(title: str, h1: str) -> float:
    """Token-set similarity ratio between title and H1."""
    def tokens(s: str) -> set[str]:
        return set(s.lower().split())
    t, h = tokens(title), tokens(h1)
    if not t or not h:
        return 0.0
    intersection = len(t & h)
    return intersection / max(len(t), len(h))


def check_heading_hierarchy(headings: list[tuple[int, str]]) -> list[dict]:
    """Return list of hierarchy violations (skipped levels)."""
    violations = []
    if not headings:
        return violations
    prev_level = headings[0][0]
    for level, text in headings[1:]:
        if level > prev_level + 1:
            violations.append({
                "skipped_from": prev_level,
                "skipped_to": level,
                "heading": text[:80],
            })
        prev_level = level
    return violations


def needs_rewrite(issues: list[str]) -> bool:
    rewrite_triggers = {
        "missing_title", "title_too_short", "title_too_long", "duplicate_title",
        "missing_description", "description_too_short", "description_too_long",
        "duplicate_description",
    }
    return bool(rewrite_triggers & set(issues))


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

def analyze(crawl: list) -> dict:
    per_page: list[dict] = []

    # First pass: collect all titles + descriptions for duplicate detection
    title_map: dict[str, list[str]] = {}    # title → [urls]
    desc_map: dict[str, list[str]] = {}     # desc → [urls]

    page_data: list[dict] = []
    for result in crawl:
        if result.get("fetch_error") or result.get("status_code", 0) >= 400:
            continue
        html = load_html(result.get("raw_html_path"))
        if not html:
            continue
        soup = get_soup(html)
        title = get_title(soup)
        description = get_meta_description(soup)
        h1s = get_h1s(soup)
        headings = get_headings(soup)
        og = get_og_tags(soup)
        twitter = get_twitter_tags(soup)

        page_data.append({
            "url": result["url"],
            "page_type": result.get("page_type", "detail:other"),
            "title": title,
            "description": description,
            "h1s": h1s,
            "headings": headings,
            "og": og,
            "twitter": twitter,
        })

        if title:
            title_map.setdefault(title, []).append(result["url"])
        if description:
            desc_map.setdefault(description, []).append(result["url"])

    duplicate_titles = {t: urls for t, urls in title_map.items() if len(urls) > 1}
    duplicate_descs = {d: urls for d, urls in desc_map.items() if len(urls) > 1}

    rewrite_requests: list[dict] = []
    summary_counts = {
        "missing_title": 0,
        "title_too_short": 0,
        "title_too_long": 0,
        "duplicate_title": 0,
        "missing_description": 0,
        "description_too_short": 0,
        "description_too_long": 0,
        "duplicate_description": 0,
        "missing_h1": 0,
        "multiple_h1s": 0,
        "h1_title_misalignment": 0,
        "heading_hierarchy_violation": 0,
        "missing_og_title": 0,
        "missing_og_description": 0,
        "missing_og_image": 0,
        "missing_twitter_card": 0,
    }

    for page in page_data:
        url = page["url"]
        title = page["title"]
        description = page["description"]
        h1s = page["h1s"]
        headings = page["headings"]
        og = page["og"]
        twitter = page["twitter"]
        page_issues: list[str] = []

        # --- Title checks ---
        if not title:
            page_issues.append("missing_title")
            summary_counts["missing_title"] += 1
        else:
            length = len(title)
            if length < TITLE_MIN:
                page_issues.append("title_too_short")
                summary_counts["title_too_short"] += 1
            elif length > TITLE_MAX:
                page_issues.append("title_too_long")
                summary_counts["title_too_long"] += 1
            if title in duplicate_titles:
                page_issues.append("duplicate_title")
                summary_counts["duplicate_title"] += 1

        # --- Meta description checks ---
        if not description:
            page_issues.append("missing_description")
            summary_counts["missing_description"] += 1
        else:
            length = len(description)
            if length < DESC_MIN:
                page_issues.append("description_too_short")
                summary_counts["description_too_short"] += 1
            elif length > DESC_MAX:
                page_issues.append("description_too_long")
                summary_counts["description_too_long"] += 1
            if description in duplicate_descs:
                page_issues.append("duplicate_description")
                summary_counts["duplicate_description"] += 1

        # --- H1 checks ---
        if not h1s:
            page_issues.append("missing_h1")
            summary_counts["missing_h1"] += 1
        else:
            if len(h1s) > 1:
                page_issues.append("multiple_h1s")
                summary_counts["multiple_h1s"] += 1
            if title:
                sim = title_similarity(title, h1s[0])
                if sim < H1_TITLE_SIMILARITY_MIN:
                    page_issues.append("h1_title_misalignment")
                    summary_counts["h1_title_misalignment"] += 1

        # --- Heading hierarchy ---
        violations = check_heading_hierarchy(headings)
        if violations:
            page_issues.append("heading_hierarchy_violation")
            summary_counts["heading_hierarchy_violation"] += len(violations)

        # --- OG tags ---
        if "og:title" not in og:
            page_issues.append("missing_og_title")
            summary_counts["missing_og_title"] += 1
        if "og:description" not in og:
            page_issues.append("missing_og_description")
            summary_counts["missing_og_description"] += 1
        if "og:image" not in og:
            page_issues.append("missing_og_image")
            summary_counts["missing_og_image"] += 1

        # --- Twitter card ---
        if "twitter:card" not in twitter:
            page_issues.append("missing_twitter_card")
            summary_counts["missing_twitter_card"] += 1

        record = {
            "url": url,
            "page_type": page["page_type"],
            "title": title,
            "title_length": len(title) if title else 0,
            "description": description,
            "description_length": len(description) if description else 0,
            "h1s": h1s,
            "heading_count": len(headings),
            "heading_hierarchy_violations": violations,
            "og_tags": list(og.keys()),
            "twitter_tags": list(twitter.keys()),
            "issues": page_issues,
        }
        per_page.append(record)

        # Package rewrite request for Content Rewriter subagent
        if needs_rewrite(page_issues):
            # Extract a body snippet for context (first 500 chars of visible text)
            html = load_html(
                next((r.get("raw_html_path") for r in crawl if r["url"] == url), None)
            )
            body_snippet = ""
            if html:
                soup = get_soup(html)
                for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
                    tag.decompose()
                body_snippet = soup.get_text(" ", strip=True)[:500]

            rewrite_requests.append({
                "url": url,
                "page_type": page["page_type"],
                "current_title": title,
                "current_description": description,
                "h1": h1s[0] if h1s else None,
                "body_snippet": body_snippet,
                "issues": [i for i in page_issues if i in {
                    "missing_title", "title_too_short", "title_too_long", "duplicate_title",
                    "missing_description", "description_too_short", "description_too_long",
                    "duplicate_description",
                }],
            })

    return {
        "per_page": per_page,
        "duplicate_titles": {t: urls for t, urls in duplicate_titles.items()},
        "duplicate_descriptions": {d[:60] + "…": urls for d, urls in duplicate_descs.items()},
        "rewrite_requests": rewrite_requests,
        "summary": summary_counts,
    }
