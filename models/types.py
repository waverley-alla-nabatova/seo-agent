from dataclasses import dataclass, field


@dataclass
class RobotsPolicy:
    disallow_patterns: list[str] = field(default_factory=list)
    crawl_delay: float = 0.0
    sitemap_urls: list[str] = field(default_factory=list)


@dataclass
class UrlRecord:
    url: str
    page_type: str          # home | section_index | detail:article | detail:job | detail:service | detail:product | detail:other
    type_confidence: float  # 0.0–1.0; below threshold → LLM fallback
    depth: int
    locale: str | None      # stripped locale prefix if detected


@dataclass
class Asset:
    url: str
    tag: str        # a | img | link | script
    attr: str       # href | src
    content_length: int | None = None
    content_type: str | None = None


@dataclass
class CrawlResult:
    url: str
    final_url: str
    redirect_chain: list[str]
    status_code: int
    raw_html_path: str | None
    rendered_html_path: str | None
    rendered: bool
    ssr_gap: bool | None        # None = not evaluated
    links: list[str]
    assets: list[Asset]
    response_time_ms: int
    page_type: str
    fetch_error: str | None = None
