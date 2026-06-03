# /seo-audit

Run a full SEO + AEO audit on a website and produce a structured Markdown report.

## Usage

```
/seo-audit <url> [options]
```

**Examples:**
```
/seo-audit https://waverleysoftware.com
/seo-audit https://example.com --max-urls 50
/seo-audit https://example.com --skip-pagespeed
```

## Options

- `--max-urls N` — cap URL discovery (default 200)
- `--skip-pagespeed` — skip PageSpeed API calls
- `--skip-render` — skip Playwright rendering (faster, no SSR check)
- `--render-sample N` — how many pages per type to render (default 3)
- `--page-type-map path/to/map.json` — override page type classification
- `--model <id>` — Claude model for LLM subagents (default: claude-sonnet-4-6)
- `--output path/to/report.md` — output file (default: `./output/<domain>-audit.md`)

## What it does

Runs a multi-agent pipeline:

1. **Discovery** — finds all URLs via sitemap + robots.txt
2. **Crawl** — fetches raw HTML; selectively renders with Playwright for SSR checks
3. **Parallel analysis** — Technical, Metadata, Schema, EEAT, AEO, PageSpeed specialists
4. **Content Rewriter subagent** — generates replacement titles, meta descriptions, JSON-LD
5. **Report Writer subagent** — synthesizes all findings into a structured Markdown audit

## Instructions for Claude

When this skill is invoked:

1. Parse the URL and any options from the arguments. Supported flags: `--max-urls`, `--skip-pagespeed`, `--skip-render`, `--render-sample`, `--output`.
2. **Validate the URL before doing anything else.** A valid URL must:
   - Start with `http://` or `https://`
   - Have a non-empty hostname containing at least one dot (e.g. `example.com`)
   - Not be a bare IP address, `localhost`, or a file path
   If the URL fails any of these checks, stop immediately and tell the user:
   > "That doesn't look like a valid website URL. Please provide a full URL starting with https://, for example: `https://example.com`"
   Do NOT start the workflow.
3. Choose the args value:
   - **No flags** (just a URL): pass `args` as the bare URL string — e.g. `args: "https://example.com"`
   - **With flags**: pass `args` as an object — e.g. `args: { url: "https://example.com", maxUrls: 50, skipPagespeed: true }`
   - Never JSON-stringify the object yourself — pass it as a real JS/JSON object value.
3. Invoke the Workflow tool with:
   - `scriptPath`: `/Users/user/Downloads/LifeOS/PROJECTS/SEO/seo-agent/.claude/workflows/seo-audit.js`
   - `args`: as determined in step 2
4. When the workflow completes, report the output file path and a one-paragraph summary of the top findings.
