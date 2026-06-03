"""
AEO Analyzer — answer-engine readiness: Q&A chunks, citability signals, chunkability.

The LLM qualitative summary is handled by a Claude Code subagent (Phase 4).
This module produces structured signal data that subagent consumes.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

QUESTION_HEADING_RE = re.compile(
    r"^\s*(how|what|why|when|where|who|which|can|is|are|does|do|should|will"
    r"|best\s+\w+|top\s+\d+|guide\s+to|introduction\s+to)\b",
    re.IGNORECASE,
)

STAT_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(%|percent|million|billion|thousand|x\s+faster|x\s+more"
    r"|times\s+(faster|more|better)|fold)\b",
    re.IGNORECASE,
)

SUMMARY_CLASS_RE = re.compile(
    r"\b(summary|tldr|tl.dr|intro|introduction|overview|key.?takeaway|abstract|lead)\b",
    re.IGNORECASE,
)

BOILERPLATE_TAGS = {"nav", "header", "footer", "aside"}

# Thresholds
ANSWER_CHUNK_MAX_CHARS = 350     # ideal answer paragraph length
ANSWER_CHUNK_MIN_CHARS = 40
WALL_OF_TEXT_CHARS = 1500        # section body with no subheadings past this → flag
BOILERPLATE_RATIO_THRESHOLD = 0.4  # boilerplate / total > this → flag
MIN_SECTION_COUNT = 3            # pages with fewer sections than this are poorly chunked


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_html(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def get_main_text(html: str) -> str:
    """Extract main content text, stripping boilerplate (trafilatura)."""
    result = trafilatura.extract(html, include_comments=False, include_tables=True)
    return result or ""


def get_total_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(" ", strip=True)


def get_boilerplate_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    parts = []
    for tag in soup.find_all(BOILERPLATE_TAGS):
        parts.append(tag.get_text(" ", strip=True))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Per-signal detectors
# ---------------------------------------------------------------------------

def detect_question_headings(soup: BeautifulSoup) -> list[dict]:
    """Find headings phrased as questions or interrogatives."""
    results = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(strip=True)
        if QUESTION_HEADING_RE.match(text) or text.strip().endswith("?"):
            # Check if immediate next sibling is a concise answer paragraph
            next_p = tag.find_next_sibling(["p", "div", "ul", "ol"])
            answer_text = next_p.get_text(strip=True) if next_p else ""
            has_answer_chunk = ANSWER_CHUNK_MIN_CHARS <= len(answer_text) <= ANSWER_CHUNK_MAX_CHARS
            results.append({
                "heading": text[:120],
                "level": int(tag.name[1]),
                "has_answer_chunk": has_answer_chunk,
                "answer_snippet": answer_text[:120] if has_answer_chunk else None,
            })
    return results


def detect_summary_block(soup: BeautifulSoup) -> dict:
    """Detect a lead summary, TL;DR, or definition paragraph near the top of main."""
    main = soup.find("main") or soup.find("article") or soup.find("body")
    if not main:
        return {"present": False, "type": None}

    # Class/id pattern
    for el in main.find_all(True):
        classes = " ".join(el.get("class", []))
        el_id = el.get("id", "")
        if SUMMARY_CLASS_RE.search(classes) or SUMMARY_CLASS_RE.search(el_id):
            return {"present": True, "type": "class_signal", "snippet": el.get_text(strip=True)[:100]}

    # First <p> that's substantive (>100 chars) and near the top
    paragraphs = main.find_all("p")
    for p in paragraphs[:3]:
        text = p.get_text(strip=True)
        if len(text) > 100:
            return {"present": True, "type": "lead_paragraph", "snippet": text[:100]}

    return {"present": False, "type": None}


def detect_chunkability(soup: BeautifulSoup) -> dict:
    """
    Assess how well the content is chunked for AI extraction.
    Returns avg section length, wall-of-text flag, section count.
    """
    main = soup.find("main") or soup.find("article") or soup.find("body")
    if not main:
        return {"section_count": 0, "avg_section_chars": 0, "has_wall_of_text": False}

    headings = main.find_all(["h1", "h2", "h3"])
    if not headings:
        # No headings at all — entire page is a wall of text
        total = len(main.get_text(strip=True))
        return {
            "section_count": 0,
            "avg_section_chars": total,
            "has_wall_of_text": total > WALL_OF_TEXT_CHARS,
        }

    # Measure text between each heading and the next
    section_lengths = []
    for i, heading in enumerate(headings):
        # Collect all text nodes until the next same-or-higher heading
        section_text = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ("h1", "h2", "h3"):
                break
            section_text.append(sibling.get_text(strip=True))
        section_lengths.append(len(" ".join(section_text)))

    avg = int(sum(section_lengths) / len(section_lengths)) if section_lengths else 0
    has_wall = any(l > WALL_OF_TEXT_CHARS for l in section_lengths)

    return {
        "section_count": len(headings),
        "avg_section_chars": avg,
        "has_wall_of_text": has_wall,
        "long_sections": sum(1 for l in section_lengths if l > WALL_OF_TEXT_CHARS),
    }


def detect_stats_with_citations(soup: BeautifulSoup) -> dict:
    """Count numeric claims that have a nearby citation link."""
    main = soup.find("main") or soup.find("article") or soup.find("body")
    if not main:
        return {"stat_count": 0, "cited_count": 0, "citation_ratio": 0.0}

    paragraphs = main.find_all(["p", "li"])
    stat_count = 0
    cited_count = 0

    for p in paragraphs:
        text = p.get_text(strip=True)
        if STAT_RE.search(text):
            stat_count += 1
            # Check if this paragraph or its siblings contain a citation link
            has_link = bool(p.find("a", href=True))
            # Also check for footnote-style reference [1], (1), ¹
            has_footnote = bool(re.search(r"(\[\d+\]|\(\d+\)|[¹²³])", text))
            if has_link or has_footnote:
                cited_count += 1

    ratio = round(cited_count / stat_count, 2) if stat_count > 0 else 0.0
    return {
        "stat_count": stat_count,
        "cited_count": cited_count,
        "citation_ratio": ratio,
    }


def detect_boilerplate_ratio(html: str) -> float:
    """Ratio of boilerplate (nav/header/footer) text to total page text."""
    total = len(get_total_text(html))
    if total == 0:
        return 0.0
    boilerplate = len(get_boilerplate_text(html))
    return round(boilerplate / total, 2)


def detect_text_only_readability(html: str, ssr_gap: bool | None) -> dict:
    """
    Estimate how well the page reads when stripped to text only.
    Uses trafilatura main-content extraction as a proxy.
    """
    main_text = get_main_text(html)
    total_text = get_total_text(html)
    if not total_text:
        return {"main_content_chars": 0, "extraction_ratio": 0.0, "readable": False}

    ratio = round(len(main_text) / len(total_text), 2) if total_text else 0.0
    readable = len(main_text) > 200 and ratio > 0.15

    # If there's an SSR gap, content collapses without JS
    if ssr_gap:
        readable = False

    return {
        "main_content_chars": len(main_text),
        "extraction_ratio": ratio,
        "readable": readable,
        "degraded_by_ssr_gap": bool(ssr_gap),
    }


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

def analyze(crawl: list) -> dict:
    per_page: list[dict] = []
    summary_counts = {
        "pages_with_question_headings": 0,
        "question_headings_with_answer_chunks": 0,
        "question_headings_without_answer_chunks": 0,
        "pages_with_summary_block": 0,
        "pages_with_wall_of_text": 0,
        "pages_poorly_chunked": 0,
        "pages_with_stats": 0,
        "pages_with_cited_stats": 0,
        "pages_not_text_readable": 0,
        "pages_with_high_boilerplate": 0,
        "faq_section_no_schema": 0,
    }

    for result in crawl:
        if result.get("fetch_error") or result.get("status_code", 0) >= 400:
            continue
        html = load_html(result.get("raw_html_path"))
        if not html:
            continue

        url = result["url"]
        page_type = result.get("page_type", "detail:other")
        soup = BeautifulSoup(html, "lxml")

        # Run all detectors
        question_headings = detect_question_headings(soup)
        summary_block = detect_summary_block(soup)
        chunkability = detect_chunkability(soup)
        stats = detect_stats_with_citations(soup)
        boilerplate_ratio = detect_boilerplate_ratio(html)
        text_readability = detect_text_only_readability(html, result.get("ssr_gap"))

        # Count summary metrics
        if question_headings:
            summary_counts["pages_with_question_headings"] += 1
            with_chunk = sum(1 for q in question_headings if q["has_answer_chunk"])
            without_chunk = len(question_headings) - with_chunk
            summary_counts["question_headings_with_answer_chunks"] += with_chunk
            summary_counts["question_headings_without_answer_chunks"] += without_chunk

        if summary_block["present"]:
            summary_counts["pages_with_summary_block"] += 1

        if chunkability["has_wall_of_text"]:
            summary_counts["pages_with_wall_of_text"] += 1

        if chunkability["section_count"] < MIN_SECTION_COUNT and page_type != "home":
            summary_counts["pages_poorly_chunked"] += 1

        if stats["stat_count"] > 0:
            summary_counts["pages_with_stats"] += 1
            if stats["cited_count"] > 0:
                summary_counts["pages_with_cited_stats"] += 1

        if not text_readability["readable"]:
            summary_counts["pages_not_text_readable"] += 1

        if boilerplate_ratio > BOILERPLATE_RATIO_THRESHOLD:
            summary_counts["pages_with_high_boilerplate"] += 1

        per_page.append({
            "url": url,
            "page_type": page_type,
            "question_headings": question_headings,
            "question_heading_count": len(question_headings),
            "answer_chunk_coverage": (
                f"{sum(1 for q in question_headings if q['has_answer_chunk'])}/{len(question_headings)}"
                if question_headings else "n/a"
            ),
            "summary_block": summary_block,
            "chunkability": chunkability,
            "stats": stats,
            "boilerplate_ratio": boilerplate_ratio,
            "text_readability": text_readability,
        })

    # Identify top pages to sample for the LLM subagent
    # Pick 3 representative pages: home, best article, best service page
    sample_pages = []
    for ptype in ("home", "detail:article", "detail:service", "section_index"):
        matches = [p for p in per_page if p["page_type"] == ptype]
        if matches:
            # Prefer pages with most question headings (most AEO-relevant)
            best = max(matches, key=lambda p: p["question_heading_count"])
            html = load_html(
                next((r.get("raw_html_path") for r in crawl if r["url"] == best["url"]), None)
            )
            sample_pages.append({
                "url": best["url"],
                "page_type": ptype,
                "main_text_snippet": get_main_text(html)[:800] if html else "",
            })

    return {
        "per_page": per_page,
        "sample_pages_for_llm": sample_pages,
        "summary": {
            **summary_counts,
            "total_pages_analyzed": len(per_page),
        },
    }
