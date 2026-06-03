"""
Crawl Agent — raw HTML fetch, selective Playwright rendering, SSR gap detection.

Per-URL output written to disk (.audit-cache/{hash}.raw.html, .rendered.html).
crawl.json holds CrawlResult records with paths, not blobs.
"""

import asyncio
import hashlib
import json
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# Playwright is imported lazily so the module loads even if not installed
_playwright_available = True
try:
    from playwright.async_api import async_playwright
except ImportError:
    _playwright_available = False


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

USER_AGENT = "SEO-Audit-Bot/1.0 (compatible; +https://github.com/seo-agent)"
REQUEST_TIMEOUT = 15       # seconds
MAX_RETRIES = 2
BASE_BACKOFF = 2.0         # seconds, doubled each retry
MIN_REQUEST_GAP = 0.5      # minimum seconds between requests to the same host

# SSR gap: if rendered visible-text is this much longer than raw visible-text, flag it
SSR_GAP_RATIO = 2.0        # rendered text is 2× longer than raw → likely CSR
SSR_GAP_MIN_CHARS = 500    # only flag if rendered has at least this much more text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def visible_text_length(html: str) -> int:
    """Approximate visible text length — strip tags, collapse whitespace."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "meta", "head"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return len(text)
    except Exception:
        return 0


def detect_ssr_gap(raw_html: str, rendered_html: str) -> bool:
    raw_len = visible_text_length(raw_html)
    rendered_len = visible_text_length(rendered_html)
    if rendered_len < SSR_GAP_MIN_CHARS:
        return False
    if raw_len == 0:
        return True
    return (rendered_len / raw_len) >= SSR_GAP_RATIO


def extract_assets(html: str, base_url: str) -> list[dict]:
    from urllib.parse import urljoin
    assets = []
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag, attr in [("a", "href"), ("img", "src"), ("link", "href"), ("script", "src")]:
            for el in soup.find_all(tag, **{attr: True}):
                val = el[attr].strip()
                if not val or val.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                absolute = urljoin(base_url, val)
                assets.append({"url": absolute, "tag": tag, "attr": attr})
    except Exception:
        pass
    return assets


def should_render(record: dict, render_counts: dict[str, int], render_sample: int, raw_html: str) -> bool:
    """Decide whether to Playwright-render this page."""
    ptype = record.get("page_type", "detail:other")

    # Always render home
    if ptype == "home":
        return True

    # Render if raw HTML looks thin (likely CSR)
    if visible_text_length(raw_html) < 800:
        return True

    # Render up to render_sample pages per page type
    if render_counts.get(ptype, 0) < render_sample:
        return True

    return False


# ---------------------------------------------------------------------------
# Per-host rate limiter
# ---------------------------------------------------------------------------

class HostLimiter:
    def __init__(self, max_concurrency: int = 5, min_gap: float = MIN_REQUEST_GAP):
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request: dict[str, float] = {}
        self._max_concurrency = max_concurrency
        self._min_gap = min_gap
        self._lock = asyncio.Lock()

    def _host(self, url: str) -> str:
        return urlparse(url).hostname or url

    async def acquire(self, url: str):
        host = self._host(url)
        async with self._lock:
            if host not in self._semaphores:
                self._semaphores[host] = asyncio.Semaphore(self._max_concurrency)
        await self._semaphores[host].acquire()
        # Enforce minimum gap between requests to the same host
        async with self._lock:
            last = self._last_request.get(host, 0)
            wait = self._min_gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)

    async def release(self, url: str):
        host = self._host(url)
        async with self._lock:
            self._last_request[host] = time.monotonic()
        self._semaphores[host].release()


# ---------------------------------------------------------------------------
# Single-URL fetch with retry
# ---------------------------------------------------------------------------

async def fetch_raw(url: str, client: httpx.AsyncClient, limiter: HostLimiter) -> dict:
    """Fetch raw HTML for one URL. Returns a partial CrawlResult dict."""
    result = {
        "url": url,
        "final_url": url,
        "redirect_chain": [],
        "status_code": 0,
        "raw_html": None,
        "response_time_ms": 0,
        "fetch_error": None,
        "assets": [],
        "links": [],
    }

    attempt = 0
    while attempt <= MAX_RETRIES:
        await limiter.acquire(url)
        t0 = time.monotonic()
        try:
            r = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            elapsed = int((time.monotonic() - t0) * 1000)
            result["status_code"] = r.status_code
            result["final_url"] = str(r.url)
            result["redirect_chain"] = [str(h.url) for h in r.history]
            result["response_time_ms"] = elapsed

            if r.status_code in (429, 503):
                # Back off and retry
                delay = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            content_type = r.headers.get("content-type", "")
            if "text/html" in content_type or not content_type:
                result["raw_html"] = r.text
                result["assets"] = extract_assets(r.text, result["final_url"])
                result["links"] = [
                    a["url"] for a in result["assets"] if a["tag"] == "a"
                ]
            return result

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            if attempt < MAX_RETRIES:
                delay = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
                attempt += 1
            else:
                result["fetch_error"] = str(e)
                return result
        except Exception as e:
            result["fetch_error"] = str(e)
            return result
        finally:
            await limiter.release(url)

    result["fetch_error"] = "Max retries exceeded"
    return result


# ---------------------------------------------------------------------------
# Playwright rendering
# ---------------------------------------------------------------------------

async def render_page(url: str, crawl_delay: float = 0.0) -> str | None:
    """Render a page with Playwright and return the full DOM HTML."""
    if not _playwright_available:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            await page.goto(url, wait_until="networkidle", timeout=30000)
            if crawl_delay > 0:
                await asyncio.sleep(crawl_delay)
            html = await page.content()
            await browser.close()
            return html
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_crawl(
    discovery: dict,
    output_dir: Path = Path(".audit-cache"),
    max_concurrency: int = 5,
    render_sample: int = 3,
    skip_render: bool = False,
) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    crawl_delay = discovery.get("robots", {}).get("crawl_delay", 0.0)
    url_records = discovery.get("urls", [])

    limiter = HostLimiter(max_concurrency=max_concurrency, min_gap=max(MIN_REQUEST_GAP, crawl_delay))
    render_counts: dict[str, int] = {}  # page_type → how many rendered so far

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    results = []

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:

        async def process_url(record: dict) -> dict:
            url = record["url"]
            ptype = record.get("page_type", "detail:other")
            h = url_hash(url)

            # --- Raw fetch ---
            raw_data = await fetch_raw(url, client, limiter)

            raw_html = raw_data.pop("raw_html", None)
            raw_html_path = None
            if raw_html:
                p = output_dir / f"{h}.raw.html"
                p.write_text(raw_html, encoding="utf-8")
                raw_html_path = str(p)

            # --- Selective render ---
            rendered_html = None
            rendered_html_path = None
            ssr_gap = None
            did_render = False

            if not skip_render and raw_html and raw_data["fetch_error"] is None:
                if should_render(record, render_counts, render_sample, raw_html):
                    render_counts[ptype] = render_counts.get(ptype, 0) + 1
                    rendered_html = await render_page(raw_data["final_url"], crawl_delay)
                    if rendered_html:
                        rp = output_dir / f"{h}.rendered.html"
                        rp.write_text(rendered_html, encoding="utf-8")
                        rendered_html_path = str(rp)
                        ssr_gap = detect_ssr_gap(raw_html, rendered_html)
                        did_render = True

            return {
                "url": url,
                "final_url": raw_data["final_url"],
                "redirect_chain": raw_data["redirect_chain"],
                "status_code": raw_data["status_code"],
                "raw_html_path": raw_html_path,
                "rendered_html_path": rendered_html_path,
                "rendered": did_render,
                "ssr_gap": ssr_gap,
                "links": raw_data["links"],
                "assets": raw_data["assets"],
                "response_time_ms": raw_data["response_time_ms"],
                "page_type": ptype,
                "fetch_error": raw_data["fetch_error"],
            }

        # Run all URLs concurrently (limiter enforces per-host cap)
        tasks = [process_url(r) for r in url_records]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            status = result["status_code"] or "ERR"
            rendered_flag = " [rendered]" if result["rendered"] else ""
            ssr_flag = " ⚠ SSR gap" if result["ssr_gap"] else ""
            print(f"  {status} {result['url']}{rendered_flag}{ssr_flag}")

    return results
