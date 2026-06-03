"""
Discovery Agent — robots.txt, sitemap, URL normalization, page-type classification.

Output schema:
{
  "root_url": str,
  "robots": { "disallow_patterns": [...], "crawl_delay": float, "sitemap_urls": [...] },
  "urls": [
    { "url": str, "page_type": str, "type_confidence": float, "depth": int, "locale": str|null }
  ],
  "ambiguous_urls": [...]   # low-confidence classifications — handed to LLM subagent
}
"""

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl
import xml.etree.ElementTree as ET

import httpx
import tldextract
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gclsrc", "fbclid", "mc_cid", "mc_eid", "ref", "_ga",
    "msclkid", "twclid", "ttclid",
})

LOCALE_PATTERN = re.compile(
    r"^/([a-z]{2}(?:-[a-z]{2,4})?)/",
    re.IGNORECASE,
)

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# Content signals for page-type detection
SIGNALS = {
    "detail:job": [
        r"\b(apply\s+now|job\s+title|salary|compensation|responsibilities|qualifications"
        r"|requirements|we['']re\s+hiring|open\s+position|join\s+our\s+team)\b"
    ],
    "detail:article": [
        r"\b(published|author|byline|reading\s+time|min\s+read|posted\s+on|updated)\b"
    ],
    "detail:service": [
        r"\b(our\s+services?|what\s+we\s+do|get\s+started|request\s+a\s+quote"
        r"|contact\s+us|learn\s+more|our\s+approach|deliverables)\b"
    ],
    "detail:product": [
        r"\b(pricing|price|buy\s+now|add\s+to\s+cart|free\s+trial|subscribe"
        r"|per\s+month|per\s+year|\$/mo|\$/yr)\b"
    ],
}

SIGNAL_RES = {k: [re.compile(p, re.IGNORECASE) for p in pats] for k, pats in SIGNALS.items()}

TYPE_CONFIDENCE_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def parse_robots(text: str, root_url: str) -> dict:
    disallow = []
    crawl_delay = 0.0
    sitemaps = []
    in_our_block = False  # applies to * or our user-agent

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            in_our_block = value in ("*", "claudebot", "claude-bot")
        elif key == "disallow" and in_our_block and value:
            disallow.append(value)
        elif key == "crawl-delay" and in_our_block:
            try:
                crawl_delay = float(value)
            except ValueError:
                pass
        elif key == "sitemap" and value:
            sitemaps.append(value)

    return {"disallow_patterns": disallow, "crawl_delay": crawl_delay, "sitemap_urls": sitemaps}


def is_disallowed(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if path.startswith(pat):
            return True
    return False


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Normalize a URL to a stable canonical form for dedup."""
    try:
        p = urlparse(url)
    except Exception:
        return url

    # Lowercase host, strip default ports
    host = p.hostname or ""
    port = p.port
    if (p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"

    # Strip tracking params, sort remaining
    params = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING_PARAMS]
    params.sort()
    query = urlencode(params)

    # Strip fragment
    return urlunparse((p.scheme, netloc, p.path, p.params, query, ""))


def dedup_key(url: str) -> str:
    """Key that treats trailing-slash variants as identical."""
    n = normalize_url(url)
    p = urlparse(n)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))


def same_domain(url: str, root: str) -> bool:
    """True if url belongs to the same registrable domain as root."""
    try:
        u = tldextract.extract(url)
        r = tldextract.extract(root)
        return u.registered_domain == r.registered_domain and u.registered_domain != ""
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Page-type classification
# ---------------------------------------------------------------------------

def strip_locale(path: str) -> tuple[str, str | None]:
    """Return (stripped_path, locale_code | None)."""
    m = LOCALE_PATTERN.match(path)
    if m:
        return path[m.end() - 1:], m.group(1).lower()
    return path, None


def url_depth(path: str) -> int:
    """Number of non-empty path segments."""
    return len([s for s in path.strip("/").split("/") if s])


def classify_from_url(url: str) -> tuple[str, float]:
    """
    Classify page type from URL structure alone.
    Returns (type_label, confidence).
    """
    p = urlparse(url)
    path, _ = strip_locale(p.path)
    depth = url_depth(path)

    if depth == 0:
        return "home", 1.0

    if depth == 1:
        return "section_index", 0.7

    # Slug-level heuristics
    slug = path.strip("/").split("/")[-1].lower()
    parent = path.strip("/").split("/")[0].lower() if depth >= 2 else ""

    job_keywords = {"job", "jobs", "career", "careers", "position", "opening", "vacancy", "vacancies", "hire", "hiring"}
    article_keywords = {"blog", "post", "article", "news", "insight", "insights", "update", "press", "podcast"}
    service_keywords = {"service", "services", "solution", "solutions", "product", "products", "offering",
                        "capability", "capabilities", "platform", "industry", "industries"}

    if parent in job_keywords or slug in job_keywords:
        return "detail:job", 0.8
    if parent in article_keywords or slug in article_keywords:
        return "detail:article", 0.75
    if parent in service_keywords:
        return "detail:service", 0.75

    # Query params or excessive depth = ambiguous
    if p.query or depth >= 4:
        return "detail:other", 0.4

    return "detail:other", 0.5


def classify_from_html(html: str) -> tuple[str, float]:
    """
    Improve classification using page content signals.
    Returns (type_label, confidence).
    """
    soup = BeautifulSoup(html, "lxml")

    # Existing schema @type is the strongest signal
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            schema_type = data.get("@type", "")
            if isinstance(schema_type, list):
                schema_type = schema_type[0] if schema_type else ""
            mapping = {
                "JobPosting": ("detail:job", 0.95),
                "Article": ("detail:article", 0.95),
                "BlogPosting": ("detail:article", 0.95),
                "Service": ("detail:service", 0.95),
                "Product": ("detail:product", 0.95),
                "WebSite": ("home", 0.95),
            }
            if schema_type in mapping:
                return mapping[schema_type]
        except (json.JSONDecodeError, AttributeError):
            continue

    # Content signal scoring
    text = soup.get_text(" ", strip=True)
    scores: dict[str, int] = {}
    for ptype, patterns in SIGNAL_RES.items():
        hits = sum(1 for pat in patterns if pat.search(text))
        if hits:
            scores[ptype] = hits

    if scores:
        best = max(scores, key=lambda k: scores[k])
        confidence = min(0.5 + scores[best] * 0.1, 0.85)
        return best, confidence

    return "detail:other", 0.4


def classify_url(url: str, html: str | None = None) -> tuple[str, float]:
    """Combine URL + optional HTML signals into a final classification."""
    url_type, url_conf = classify_from_url(url)

    if html and url_conf < TYPE_CONFIDENCE_THRESHOLD:
        html_type, html_conf = classify_from_html(html)
        if html_conf > url_conf:
            return html_type, html_conf

    return url_type, url_conf


# ---------------------------------------------------------------------------
# Sitemap fetching
# ---------------------------------------------------------------------------

async def fetch_sitemap_urls(sitemap_url: str, client: httpx.AsyncClient, visited: set) -> list[str]:
    """Recursively fetch URLs from a sitemap or sitemap index."""
    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)

    try:
        r = await client.get(sitemap_url, timeout=15, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return []

    urls = []
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return []

    tag = root.tag.lower()

    # Sitemap index — recurse
    if "sitemapindex" in tag:
        for loc in root.iter(f"{SITEMAP_NS}loc"):
            child_url = (loc.text or "").strip()
            if child_url:
                urls.extend(await fetch_sitemap_urls(child_url, client, visited))
    else:
        # Regular sitemap
        for loc in root.iter(f"{SITEMAP_NS}loc"):
            u = (loc.text or "").strip()
            if u:
                urls.append(u)

    return urls


async def discover_via_crawl(root_url: str, client: httpx.AsyncClient) -> list[str]:
    """Fallback: extract links from the homepage when no sitemap exists."""
    try:
        r = await client.get(root_url, timeout=15, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute = urljoin(root_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https"):
            urls.append(absolute)
    return urls


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_discovery(
    root_url: str,
    max_urls: int = 200,
    page_type_map: Path | None = None,
) -> dict:
    # Load user overrides
    type_overrides: dict[str, str] = {}
    if page_type_map and Path(page_type_map).exists():
        type_overrides = json.loads(Path(page_type_map).read_text())

    # Ensure root URL has a scheme
    if not root_url.startswith(("http://", "https://")):
        root_url = "https://" + root_url

    parsed_root = urlparse(root_url)
    root_base = f"{parsed_root.scheme}://{parsed_root.netloc}"

    headers = {"User-Agent": "SEO-Audit-Bot/1.0 (compatible; +https://github.com/seo-agent)"}

    async with httpx.AsyncClient(headers=headers) as client:
        # 1. robots.txt
        robots_text = ""
        try:
            r = await client.get(f"{root_base}/robots.txt", timeout=10, follow_redirects=True)
            if r.status_code == 200:
                robots_text = r.text
        except Exception:
            pass
        robots = parse_robots(robots_text, root_base)

        # 2. Sitemap(s)
        sitemap_urls = robots["sitemap_urls"]
        if not sitemap_urls:
            # Try common sitemap locations
            for candidate in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml"]:
                candidate_url = root_base + candidate
                try:
                    r = await client.head(candidate_url, timeout=5, follow_redirects=True)
                    if r.status_code == 200:
                        sitemap_urls = [candidate_url]
                        break
                except Exception:
                    continue

        visited_sitemaps: set[str] = set()
        raw_urls: list[str] = []

        if sitemap_urls:
            for su in sitemap_urls:
                raw_urls.extend(await fetch_sitemap_urls(su, client, visited_sitemaps))

        # 3. Fallback to homepage link crawl
        if not raw_urls:
            raw_urls = await discover_via_crawl(root_url, client)

        # Always include root
        raw_urls.append(root_url)

    # 4. Filter, normalize, deduplicate
    seen: set[str] = set()
    filtered: list[str] = []
    for u in raw_urls:
        if not same_domain(u, root_url):
            continue
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https"):
            continue
        if is_disallowed(parsed.path, robots["disallow_patterns"]):
            continue
        key = dedup_key(u)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(normalize_url(u))

    filtered = filtered[:max_urls]

    # 5. Classify
    url_records = []
    ambiguous = []

    for u in filtered:
        parsed = urlparse(u)
        path, locale = strip_locale(parsed.path)
        depth = url_depth(path)

        # User overrides win
        override_type = None
        for prefix, ptype in type_overrides.items():
            if parsed.path.startswith(prefix):
                override_type = ptype
                break

        if override_type:
            ptype, confidence = override_type, 1.0
        else:
            ptype, confidence = classify_from_url(u)

        record = {
            "url": u,
            "page_type": ptype,
            "type_confidence": round(confidence, 2),
            "depth": depth,
            "locale": locale,
        }
        url_records.append(record)

        if confidence < TYPE_CONFIDENCE_THRESHOLD and not override_type:
            ambiguous.append({"url": u, "depth": depth, "path": parsed.path})

    return {
        "root_url": root_url,
        "robots": robots,
        "urls": url_records,
        "ambiguous_urls": ambiguous,
        "stats": {
            "total": len(url_records),
            "ambiguous": len(ambiguous),
            "by_type": _count_by_type(url_records),
        },
    }


def _count_by_type(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        counts[r["page_type"]] = counts.get(r["page_type"], 0) + 1
    return counts
