"""
Technical Analyzer — SSR gaps, canonicals, redirects, broken links, large assets.

All checks are programmatic. Output is a structured dict ready for the report.
"""

import asyncio
import random
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup


USER_AGENT = "SEO-Audit-Bot/1.0 (compatible; +https://github.com/seo-agent)"
HEAD_TIMEOUT = 20
LINK_CHECK_TIMEOUT = 20
MAX_RETRIES = 2
LARGE_ASSET_BYTES = 5 * 1024 * 1024   # 5 MB
LARGE_VIDEO_BYTES = 5 * 1024 * 1024
ASSET_EXTENSIONS = {
    "video": {".mp4", ".webm", ".ogv", ".mov", ".avi"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"},
}


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def load_html(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def get_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def get_canonical(soup: BeautifulSoup) -> str | None:
    tag = soup.find("link", rel=lambda r: r and "canonical" in r)
    if tag and tag.get("href"):
        return tag["href"].strip()
    return None


def get_viewport(soup: BeautifulSoup) -> bool:
    return bool(soup.find("meta", attrs={"name": "viewport"}))


def get_lang(soup: BeautifulSoup) -> str | None:
    html_tag = soup.find("html")
    if html_tag:
        return html_tag.get("lang") or None
    return None


def normalize_url_for_compare(url: str) -> str:
    """Strip trailing slash for loose canonical comparison."""
    return url.rstrip("/").lower()


# ---------------------------------------------------------------------------
# Asset size check via HEAD
# ---------------------------------------------------------------------------

async def check_asset_size(url: str, client: httpx.AsyncClient) -> int | None:
    """Return Content-Length in bytes, or None if unavailable."""
    try:
        r = await client.head(url, timeout=HEAD_TIMEOUT, follow_redirects=True)
        cl = r.headers.get("content-length")
        return int(cl) if cl else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Two-pass broken link checker
# ---------------------------------------------------------------------------

async def check_link_status(url: str, client: httpx.AsyncClient) -> int | None:
    """
    Check a URL via HEAD (GET fallback).
    Returns HTTP status code, or None if the request timed out / network error
    (None = inconclusive, not reported as broken).
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await client.head(url, timeout=LINK_CHECK_TIMEOUT, follow_redirects=True)
            if r.status_code == 405:
                r = await client.get(url, timeout=LINK_CHECK_TIMEOUT, follow_redirects=True)
            return r.status_code
        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout):
            # Timeout is inconclusive — slow server ≠ broken link
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.5))
            else:
                return None   # inconclusive
        except Exception:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BASE_BACKOFF + random.uniform(0, 0.5))
            else:
                return None   # inconclusive
    return None


BASE_BACKOFF = 1.0


def is_internal(url: str, root_url: str) -> bool:
    try:
        import tldextract
        u = tldextract.extract(url)
        r = tldextract.extract(root_url)
        return u.registered_domain == r.registered_domain and u.registered_domain != ""
    except Exception:
        return urlparse(url).netloc == urlparse(root_url).netloc


def ext(url: str) -> str:
    path = urlparse(url).path.lower()
    return Path(path).suffix


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

def analyze(crawl: list) -> dict:
    return asyncio.run(_analyze(crawl))


async def _analyze(crawl: list) -> dict:
    root_url = crawl[0]["url"] if crawl else ""
    crawled_status: dict[str, int] = {r["url"]: r["status_code"] for r in crawl}
    crawled_final: dict[str, str] = {r["url"]: r["final_url"] for r in crawl}

    issues = {
        "ssr_gaps": [],
        "missing_canonical": [],
        "canonical_mismatch": [],
        "long_redirect_chains": [],
        "trailing_slash_redirects": [],
        "broken_internal_links": [],
        "undefined_links": [],
        "large_assets": [],
        "missing_viewport": [],
        "missing_lang": [],
    }

    # Collect unchecked internal links for the HEAD pass
    unchecked_links: set[str] = set()
    # Map link → list of pages that contain it
    link_sources: dict[str, list[str]] = {}

    for result in crawl:
        url = result["url"]
        html = load_html(result.get("raw_html_path"))
        if not html:
            continue
        soup = get_soup(html)

        # --- SSR gap ---
        if result.get("ssr_gap"):
            issues["ssr_gaps"].append({"url": url})

        # --- Canonical ---
        canonical = get_canonical(soup)
        if not canonical:
            issues["missing_canonical"].append({"url": url})
        else:
            if normalize_url_for_compare(canonical) != normalize_url_for_compare(result["final_url"]):
                issues["canonical_mismatch"].append({
                    "url": url,
                    "canonical": canonical,
                    "final_url": result["final_url"],
                })

        # --- Redirect chains ---
        chain = result.get("redirect_chain", [])
        if len(chain) > 1:
            issues["long_redirect_chains"].append({"url": url, "chain": chain})
        # Trailing slash redirect: chain has exactly one hop and paths differ only by /
        if len(chain) == 1:
            src = urlparse(url).path
            dst = urlparse(result["final_url"]).path
            if src.rstrip("/") == dst.rstrip("/") and src != dst:
                issues["trailing_slash_redirects"].append({
                    "url": url,
                    "final_url": result["final_url"],
                })

        # --- viewport / lang ---
        if not get_viewport(soup):
            issues["missing_viewport"].append({"url": url})
        if not get_lang(soup):
            issues["missing_lang"].append({"url": url})

        # --- Collect internal links for broken-link check ---
        for link in result.get("links", []):
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https"):
                continue
            if not is_internal(link, root_url):
                continue
            # Strip fragment
            clean = link.split("#")[0]
            if not clean:
                continue

            # /undefined pattern
            if parsed.path == "/undefined" or parsed.path.endswith("/undefined"):
                issues["undefined_links"].append({"url": url, "broken_href": link})
                continue

            if clean not in crawled_status:
                unchecked_links.add(clean)
            if clean not in link_sources:
                link_sources[clean] = []
            link_sources[clean].append(url)

        # --- Large assets ---
        for asset in result.get("assets", []):
            asset_url = asset.get("url", "")
            suffix = ext(asset_url)
            is_video = suffix in ASSET_EXTENSIONS["video"]
            is_image = suffix in ASSET_EXTENSIONS["image"]
            if not (is_video or is_image):
                continue
            threshold = LARGE_VIDEO_BYTES if is_video else LARGE_ASSET_BYTES
            cl = asset.get("content_length")
            if cl and cl > threshold:
                issues["large_assets"].append({
                    "page": url,
                    "asset": asset_url,
                    "size_mb": round(cl / 1024 / 1024, 1),
                    "type": "video" if is_video else "image",
                })

    # --- Pass 1: resolve already-crawled links ---
    broken_from_crawl = []
    for link, sources in link_sources.items():
        if link in crawled_status and crawled_status[link] >= 400:
            broken_from_crawl.append({
                "href": link,
                "status": crawled_status[link],
                "found_on": sources[:5],
            })

    # --- Pass 2: HEAD-check unchecked internal links ---
    broken_from_head = []
    if unchecked_links:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(headers=headers) as client:
            sem = asyncio.Semaphore(10)

            async def check(link: str):
                async with sem:
                    status = await check_link_status(link, client)
                    if status is not None and status >= 400:
                        broken_from_head.append({
                            "href": link,
                            "status": status,
                            "found_on": link_sources.get(link, [])[:5],
                        })

            await asyncio.gather(*[check(lnk) for lnk in unchecked_links])

    issues["broken_internal_links"] = broken_from_crawl + broken_from_head

    # --- Large asset HEAD pass (for assets without Content-Length in crawl) ---
    no_cl_assets = [
        (r["url"], a)
        for r in crawl
        for a in r.get("assets", [])
        if ext(a.get("url", "")) in ASSET_EXTENSIONS["video"]
        and not a.get("content_length")
    ]
    if no_cl_assets:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
            sem = asyncio.Semaphore(5)

            async def check_asset(page_url: str, asset: dict):
                async with sem:
                    size = await check_asset_size(asset["url"], client)
                    if size and size > LARGE_VIDEO_BYTES:
                        issues["large_assets"].append({
                            "page": page_url,
                            "asset": asset["url"],
                            "size_mb": round(size / 1024 / 1024, 1),
                            "type": "video",
                        })

            await asyncio.gather(*[check_asset(p, a) for p, a in no_cl_assets])

    # --- Dedup large assets ---
    seen_assets: set[str] = set()
    deduped = []
    for item in issues["large_assets"]:
        key = item["asset"]
        if key not in seen_assets:
            seen_assets.add(key)
            deduped.append(item)
    issues["large_assets"] = deduped

    return {
        "issues": issues,
        "summary": {
            "ssr_gaps": len(issues["ssr_gaps"]),
            "missing_canonical": len(issues["missing_canonical"]),
            "canonical_mismatch": len(issues["canonical_mismatch"]),
            "long_redirect_chains": len(issues["long_redirect_chains"]),
            "trailing_slash_redirects": len(issues["trailing_slash_redirects"]),
            "broken_internal_links": len(issues["broken_internal_links"]),
            "undefined_links": len(issues["undefined_links"]),
            "large_assets": len(issues["large_assets"]),
            "missing_viewport": len(issues["missing_viewport"]),
            "missing_lang": len(issues["missing_lang"]),
        },
    }
