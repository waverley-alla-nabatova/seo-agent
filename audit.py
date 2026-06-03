"""
SEO + AEO Audit — CLI entry point.

Each subcommand corresponds to one pipeline stage so agents can invoke
individual modules directly. The `run` command executes the full pipeline
(intended to be called from the Claude Code workflow).
"""

import asyncio
import json
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)


@app.command()
def discover(
    url: str,
    output: Path = typer.Option(Path(".audit-cache/discovery.json"), help="Output path"),
    max_urls: int = typer.Option(200, help="URL cap"),
    page_type_map: Path | None = typer.Option(None, help="JSON path-prefix→type overrides"),
):
    """Discover URLs, parse robots.txt + sitemap, classify page types."""
    from modules.discovery import run_discovery
    result = asyncio.run(run_discovery(url, max_urls=max_urls, page_type_map=page_type_map))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"Discovered {len(result['urls'])} URLs → {output}")


@app.command()
def crawl(
    discovery: Path = typer.Option(Path(".audit-cache/discovery.json")),
    output_dir: Path = typer.Option(Path(".audit-cache")),
    max_concurrency: int = typer.Option(5),
    render_sample: int = typer.Option(3, help="Max pages to render per page type"),
    skip_render: bool = typer.Option(False),
):
    """Fetch raw HTML (and selectively rendered HTML) for all discovered URLs."""
    from modules.crawl import run_crawl
    discovery_data = json.loads(discovery.read_text())
    results = asyncio.run(run_crawl(
        discovery_data,
        output_dir=output_dir,
        max_concurrency=max_concurrency,
        render_sample=render_sample,
        skip_render=skip_render,
    ))
    out = output_dir / "crawl.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    typer.echo(f"Crawled {len(results)} URLs → {out}")


@app.command()
def analyze(
    module: str = typer.Argument(help="technical | metadata | schema | eeat | aeo | pagespeed"),
    crawl_data: Path = typer.Option(Path(".audit-cache/crawl.json")),
    output_dir: Path = typer.Option(Path(".audit-cache")),
    pagespeed_key: str | None = typer.Option(None, envvar="PAGESPEED_API_KEY"),
):
    """Run a single specialist analyzer and write its JSON report."""
    from modules import technical, metadata, schema, eeat, aeo, pagespeed as ps

    crawl = json.loads(crawl_data.read_text())
    dispatch = {
        "technical": technical.analyze,
        "metadata": metadata.analyze,
        "schema": schema.analyze,
        "eeat": eeat.analyze,
        "aeo": aeo.analyze,
        "pagespeed": lambda c: asyncio.run(ps.analyze(c, api_key=pagespeed_key)),
    }
    if module not in dispatch:
        typer.echo(f"Unknown module: {module}. Choose from: {', '.join(dispatch)}", err=True)
        raise typer.Exit(1)

    fn = dispatch[module]
    result = fn(crawl) if module != "pagespeed" else dispatch[module](crawl)
    out = output_dir / f"{module}.json"
    out.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"{module} report → {out}")


if __name__ == "__main__":
    app()
