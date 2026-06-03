"""
Merge rewrites_titles.json + rewrites_schemas.json → rewrites.json.
Run after the Content Rewriter and Schema Generator subagents complete.
"""

import json
import sys
from pathlib import Path


def merge(cache_dir: str = ".audit-cache") -> dict:
    cache = Path(cache_dir)

    titles_path = cache / "rewrites_titles.json"
    schemas_path = cache / "rewrites_schemas.json"

    titles_map: dict[str, dict] = {}
    schemas_map: dict[str, list] = {}

    if titles_path.exists():
        try:
            data = json.loads(titles_path.read_text())
            for item in data.get("rewrites", []):
                url = item.get("url", "")
                if url:
                    titles_map[url] = {
                        "title": item.get("title"),
                        "description": item.get("description"),
                    }
        except (json.JSONDecodeError, KeyError):
            pass

    if schemas_path.exists():
        try:
            data = json.loads(schemas_path.read_text())
            for item in data.get("schemas", []):
                url = item.get("url", "")
                if url:
                    if url not in schemas_map:
                        schemas_map[url] = []
                    schemas_map[url].append({
                        "type": item.get("type"),
                        "jsonld": item.get("jsonld"),
                    })
        except (json.JSONDecodeError, KeyError):
            pass

    all_urls = set(titles_map.keys()) | set(schemas_map.keys())
    rewrites = {
        url: {
            "title": titles_map.get(url, {}).get("title"),
            "description": titles_map.get(url, {}).get("description"),
            "schemas": schemas_map.get(url, []),
        }
        for url in all_urls
    }

    result = {
        "rewrites": rewrites,
        "stats": {
            "title_rewrites": len(titles_map),
            "description_rewrites": sum(
                1 for v in titles_map.values() if v.get("description")
            ),
            "schema_blocks": sum(len(v) for v in schemas_map.values()),
            "total_urls": len(rewrites),
        },
    }

    out = cache / "rewrites.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"rewrites.json → {result['stats']}")
    return result


if __name__ == "__main__":
    cache = sys.argv[1] if len(sys.argv) > 1 else ".audit-cache"
    merge(cache)
