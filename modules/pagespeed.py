"""
PageSpeed Agent — Google PageSpeed Insights API v5.

Runs mobile + desktop for each URL. Enforces rate limit (25 req/100s).
Skipped gracefully if no API key is provided.
"""

import asyncio
import time
from urllib.parse import urlparse

import httpx


API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Google allows 25 requests per 100 seconds
RATE_LIMIT_REQUESTS = 25
RATE_LIMIT_WINDOW = 100.0

# Scores below these are flagged
THRESHOLD_DESKTOP = 90
THRESHOLD_MOBILE = 75

# Core Web Vitals + key audits to extract
METRIC_KEYS = [
    "first-contentful-paint",
    "largest-contentful-paint",
    "total-blocking-time",
    "cumulative-layout-shift",
    "speed-index",
    "interactive",
]

# Audits that map to actionable recommendations
ACTIONABLE_AUDITS = [
    "render-blocking-resources",
    "unused-javascript",
    "unused-css-rules",
    "uses-optimized-images",
    "uses-responsive-images",
    "uses-webp-images",
    "efficient-animated-content",
    "unminified-javascript",
    "unminified-css",
    "uses-long-cache-ttl",
    "total-byte-weight",
    "largest-contentful-paint-element",
    "lcp-lazy-loaded",
    "no-document-write",
    "offscreen-images",
]


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate: int, window: float):
        self._rate = rate
        self._window = window
        self._tokens = rate
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens proportionally
            refill = (elapsed / self._window) * self._rate
            self._tokens = min(self._rate, self._tokens + refill)
            self._last_refill = now

            if self._tokens < 1:
                wait = (1 - self._tokens) / (self._rate / self._window)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def extract_metrics(result: dict) -> dict:
    audits = result.get("lighthouseResult", {}).get("audits", {})
    metrics = {}
    for key in METRIC_KEYS:
        audit = audits.get(key, {})
        metrics[key] = {
            "display_value": audit.get("displayValue"),
            "numeric_value": audit.get("numericValue"),
            "score": audit.get("score"),
        }
    return metrics


def extract_failing_audits(result: dict, max_items: int = 8) -> list[dict]:
    audits = result.get("lighthouseResult", {}).get("audits", {})
    failing = []
    for key in ACTIONABLE_AUDITS:
        audit = audits.get(key, {})
        score = audit.get("score")
        if score is not None and score < 0.9:
            failing.append({
                "id": key,
                "title": audit.get("title", key),
                "score": score,
                "display_value": audit.get("displayValue"),
            })
    # Sort by score ascending (worst first)
    failing.sort(key=lambda a: a["score"] if a["score"] is not None else -1)
    return failing[:max_items]


async def fetch_pagespeed(
    url: str,
    strategy: str,
    api_key: str,
    client: httpx.AsyncClient,
    bucket: TokenBucket,
) -> dict:
    await bucket.acquire()
    params = {
        "url": url,
        "strategy": strategy,
        "key": api_key,
        "category": "performance",
    }
    try:
        r = await client.get(API_URL, params=params, timeout=30)
        if r.status_code == 429:
            # Back off and retry once
            await asyncio.sleep(10)
            await bucket.acquire()
            r = await client.get(API_URL, params=params, timeout=30)

        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "url": url, "strategy": strategy}

        data = r.json()
        categories = data.get("lighthouseResult", {}).get("categories", {})
        score = categories.get("performance", {}).get("score")

        return {
            "url": url,
            "strategy": strategy,
            "score": round(score * 100) if score is not None else None,
            "below_threshold": (
                score is not None and (
                    (strategy == "desktop" and score * 100 < THRESHOLD_DESKTOP) or
                    (strategy == "mobile" and score * 100 < THRESHOLD_MOBILE)
                )
            ),
            "metrics": extract_metrics(data),
            "failing_audits": extract_failing_audits(data),
            "error": None,
        }
    except Exception as e:
        return {"error": str(e), "url": url, "strategy": strategy}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def analyze(crawl: list, api_key: str | None = None) -> dict:
    if not api_key or not api_key.strip():
        return {
            "skipped": True,
            "reason": "No PAGESPEED_API_KEY provided",
            "results": [],
            "summary": {},
        }

    # Deduplicate URLs, prioritise key page types
    type_priority = {
        "home": 0,
        "detail:service": 1,
        "section_index": 2,
        "detail:article": 3,
        "detail:job": 4,
        "detail:product": 5,
        "detail:other": 6,
    }
    urls_to_check = sorted(
        [r["url"] for r in crawl if not r.get("fetch_error") and r.get("status_code") == 200],
        key=lambda u: type_priority.get(
            next((r["page_type"] for r in crawl if r["url"] == u), "detail:other"), 6
        ),
    )

    bucket = TokenBucket(rate=RATE_LIMIT_REQUESTS, window=RATE_LIMIT_WINDOW)
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        # Two tasks per URL: mobile + desktop
        tasks = []
        for url in urls_to_check:
            for strategy in ("mobile", "desktop"):
                tasks.append(fetch_pagespeed(url, strategy, api_key, client, bucket))

        raw = await asyncio.gather(*tasks, return_exceptions=True)

    # Pair mobile + desktop results per URL
    by_url: dict[str, dict] = {}
    for item in raw:
        if isinstance(item, Exception):
            continue
        url = item.get("url", "")
        strategy = item.get("strategy", "")
        if url not in by_url:
            by_url[url] = {}
        by_url[url][strategy] = item

    for url, strategies in by_url.items():
        mobile = strategies.get("mobile", {})
        desktop = strategies.get("desktop", {})
        page_type = next((r["page_type"] for r in crawl if r["url"] == url), "unknown")
        results.append({
            "url": url,
            "page_type": page_type,
            "mobile": mobile,
            "desktop": desktop,
            "below_threshold": (
                mobile.get("below_threshold", False) or
                desktop.get("below_threshold", False)
            ),
        })

    # Summary
    scored = [r for r in results if not r["mobile"].get("error") and not r["desktop"].get("error")]
    below = [r for r in results if r["below_threshold"]]

    mobile_scores = [r["mobile"]["score"] for r in scored if r["mobile"].get("score") is not None]
    desktop_scores = [r["desktop"]["score"] for r in scored if r["desktop"].get("score") is not None]

    # Collect all failing audits across pages for site-wide top issues
    all_failing: dict[str, dict] = {}
    for r in results:
        for strategy in ("mobile", "desktop"):
            for audit in r.get(strategy, {}).get("failing_audits", []):
                key = audit["id"]
                if key not in all_failing:
                    all_failing[key] = {**audit, "affected_pages": 0}
                all_failing[key]["affected_pages"] += 1

    top_issues = sorted(all_failing.values(), key=lambda a: -a["affected_pages"])[:8]

    return {
        "skipped": False,
        "results": results,
        "top_site_issues": top_issues,
        "summary": {
            "pages_checked": len(results),
            "pages_below_threshold": len(below),
            "avg_mobile_score": round(sum(mobile_scores) / len(mobile_scores)) if mobile_scores else None,
            "avg_desktop_score": round(sum(desktop_scores) / len(desktop_scores)) if desktop_scores else None,
            "mobile_threshold": THRESHOLD_MOBILE,
            "desktop_threshold": THRESHOLD_DESKTOP,
            "errors": sum(1 for r in results if r["mobile"].get("error") or r["desktop"].get("error")),
        },
    }
