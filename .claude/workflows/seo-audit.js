export const meta = {
  name: 'seo-audit',
  description: 'Full SEO + AEO audit — crawl, analyze, generate, report',
  phases: [
    { title: 'Discover' },
    { title: 'Crawl' },
    { title: 'Analyze' },
    { title: 'Generate' },
    { title: 'Report' },
  ],
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const ROOT = '/Users/user/Downloads/LifeOS/PROJECTS/SEO/seo-agent'
const CACHE = `${ROOT}/.audit-cache`
const OUTPUT_DIR = `${ROOT}/output`

// Accept args as an object, a JSON-stringified object, or a bare URL string.
let a = args
if (typeof a === 'string') {
  const s = a.trim()
  if (s.startsWith('{')) {
    try { a = JSON.parse(s) } catch (_) { /* leave as bare string */ }
  }
}
const url = typeof a === 'string' ? a : (a && a.url ? a.url : '')
if (!url) throw new Error('url is required — pass as args.url or as a plain string')

const maxUrls      = (a && a.maxUrls)      || 200
const skipRender   = (a && a.skipRender)   || false
const renderSample = (a && a.renderSample) || 3
const skipPagespeed= (a && a.skipPagespeed)|| false
const outputFile   = (a && a.output) || (() => {
  const domain = url.replace(/https?:\/\//, '').replace(/\//g, '').replace(/\./g, '-')
  return `${OUTPUT_DIR}/${domain}-audit.md`
})()

const renderFlag = skipRender ? '--skip-render' : `--render-sample ${renderSample}`
const psFlag     = skipPagespeed ? '--skip-pagespeed' : ''

log(`Auditing: ${url}`)
log(`Output:   ${outputFile}`)

// ---------------------------------------------------------------------------
// Phase 1 — Discover
// ---------------------------------------------------------------------------

phase('Discover')

await agent(
  `Run this exact command and report the number of URLs discovered and how many are ambiguous:

cd ${ROOT} && uv run python audit.py discover "${url}" --output ${CACHE}/discovery.json --max-urls ${maxUrls} 2>&1`,
  { label: 'discover', phase: 'Discover' }
)

// ---------------------------------------------------------------------------
// Phase 2 — Crawl
// ---------------------------------------------------------------------------

phase('Crawl')

await agent(
  `Run this exact command and report how many URLs were crawled, how many rendered, and how many errors:

cd ${ROOT} && uv run python audit.py crawl --discovery ${CACHE}/discovery.json --output-dir ${CACHE} --max-concurrency 5 ${renderFlag} 2>&1`,
  { label: 'crawl', phase: 'Crawl' }
)

// Fail loud if crawl produced no results — prevents a silent empty report
const crawlCheck = await agent(
  `Run: node -e "const c=require('${CACHE}/crawl.json');process.exit(c.length===0?1:0)" 2>/dev/null || echo EMPTY
Also run: node -e "const d=require('${CACHE}/discovery.json');console.log(d.urls.length+' URLs discovered')" 2>/dev/null
Report both outputs.`,
  {
    label: 'crawl-guard',
    phase: 'Crawl',
    schema: { type: 'object', properties: { empty: { type: 'boolean' }, url_count: { type: 'number' } }, required: ['empty'] },
  }
)
if (crawlCheck && crawlCheck.empty) {
  throw new Error(`Crawl returned 0 pages for ${url}. Check the URL is reachable and the sitemap exists.`)
}

// ---------------------------------------------------------------------------
// Phase 3 — Analyze (all specialists in parallel)
// ---------------------------------------------------------------------------

phase('Analyze')

const analyzeModules = ['technical', 'metadata', 'schema', 'eeat', 'aeo']
if (!skipPagespeed) analyzeModules.push('pagespeed')

await parallel(analyzeModules.map(mod => () =>
  agent(
    `Run this command and report the key findings count:

cd ${ROOT} && uv run python audit.py analyze ${mod} --crawl-data ${CACHE}/crawl.json --output-dir ${CACHE} ${mod === 'pagespeed' ? '--pagespeed-key $PAGESPEED_API_KEY' : ''} 2>&1`,
    { label: `analyze:${mod}`, phase: 'Analyze' }
  )
))

// ---------------------------------------------------------------------------
// Phase 4 — Generate (LLM steps)
// ---------------------------------------------------------------------------

phase('Generate')

// 4a. Optional: classify ambiguous page types
const classified = await agent(
  `Read the file ${CACHE}/discovery.json using the Read tool.
Look at the ambiguous_urls array.

If it is empty, return {"count": 0, "classifications": []}.

Otherwise classify each ambiguous URL by its page type. Use the URL path as your
primary signal. Valid types: home | section_index | detail:article | detail:job |
detail:service | detail:product | detail:other

Return your result.`,
  {
    label: 'classify-ambiguous',
    phase: 'Generate',
    schema: {
      type: 'object',
      properties: {
        count: { type: 'number' },
        classifications: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              url:         { type: 'string' },
              page_type:   { type: 'string' },
              confidence:  { type: 'number' },
            },
            required: ['url', 'page_type', 'confidence'],
          },
        },
      },
      required: ['count', 'classifications'],
    },
  }
)

if (classified && classified.count > 0) {
  await agent(
    `Read ${CACHE}/discovery.json.
For each entry in the urls array, if its url matches one of these classifications,
update its page_type and type_confidence:

${JSON.stringify(classified.classifications, null, 2)}

Write the updated JSON back to ${CACHE}/discovery.json.`,
    { label: 'update-page-types', phase: 'Generate' }
  )
}

// 4b–4e: content rewriter, schema generator, EEAT summary, AEO summary — all parallel
await parallel([

  // Content Rewriter — titles + meta descriptions
  () => agent(
    `You are an expert SEO copywriter generating optimized titles and meta descriptions.

Read ${CACHE}/metadata.json using the Read tool. Find the rewrite_requests array.

For EACH request generate a new title and description following these rules:

TITLE (30–60 chars, strict):
- Specific to the page topic — never generic
- Brand name at end after " — " separator
- Templates by page_type:
  - detail:service / section_index → "[Service] Services — [Brand]" or "[Topic] — [Brand]"
  - detail:article                  → "[Specific Topic] — [Brand]"
  - detail:job                      → "[Job Title] — Open Position — [Brand]"
  - detail:other                    → "[Page Topic] — [Brand]"
- No duplicates across pages

DESCRIPTION (120–158 chars, strict):
- Describes what the visitor gets from the page
- Action-oriented, includes a soft outcome or CTA
- No keyword stuffing

Use current_title, h1, page_type, and body_snippet from each request as context.
If brand name is unclear, infer it from the URL domain.

Write the result to ${CACHE}/rewrites_titles.json as exactly:
{
  "rewrites": [
    { "url": "...", "title": "...", "description": "..." }
  ]
}`,
    { label: 'content-rewriter', phase: 'Generate' }
  ),

  // Schema Generator — missing JSON-LD blocks
  () => agent(
    `You are a schema.org expert generating JSON-LD structured data.

Read ${CACHE}/schema.json using the Read tool. Find the generation_requests array.

For each request, generate every schema type listed in missing_types.
Use context.title, context.description, context.h1, context.body_snippet, and the url.

Rules per type:
- BreadcrumbList: derive path segments from the URL. Position starts at 1 for home.
- Service: name = page title, provider = { "@type": "Organization", "name": inferred brand, "url": site root }
- Article / BlogPosting: headline = h1 or title, author = { "@type": "Person", "name": infer from body or omit }
- JobPosting: title = h1, hiringOrganization = { "@type": "Organization", "name": brand }, datePosted = today if unknown
- Organization / WebSite: only generate for home page, use all available context
- FAQPage: only generate if context.faq_section_present is true; infer Q&A pairs from body_snippet

Output valid JSON objects (not strings). No comments, no trailing commas.

Write to ${CACHE}/rewrites_schemas.json as exactly:
{
  "schemas": [
    { "url": "...", "type": "BreadcrumbList", "jsonld": { ...schema object... } }
  ]
}`,
    { label: 'schema-generator', phase: 'Generate' }
  ),

  // EEAT qualitative summary
  () => agent(
    `You are an SEO consultant specialising in Google's E-E-A-T framework
(Experience, Expertise, Authoritativeness, Trustworthiness).

Read ${CACHE}/eeat.json using the Read tool. Review site_signals, gaps, and
programmatic_recommendations.

Write a concise qualitative assessment:
- 3–5 bullet points — what signals are strong, what is missing
- Be specific: name the actual signals found or absent
- Prioritised list of exactly 3 recommendations, most impactful first

Write to ${CACHE}/eeat_summary.json as:
{
  "assessment": "...",
  "top_recommendations": ["...", "...", "..."]
}`,
    { label: 'eeat-summary', phase: 'Generate' }
  ),

  // AEO qualitative summary
  () => agent(
    `You are an expert in Answer Engine Optimisation (AEO) — making content
extractable and citable by AI answer engines such as Perplexity, ChatGPT,
and Google AI Overviews.

Read ${CACHE}/aeo.json using the Read tool. Review summary, llms_txt, and
sample_pages_for_llm.

Write a concise qualitative AEO assessment:
- 3–5 bullet points on how well this site can be cited/extracted by AI engines
- Reference specific numbers (e.g. "319 question headings lack answer chunks")
- Comment on: Q&A chunk coverage, wall-of-text pages, citability signals
- Prioritised list of exactly 3 concrete, actionable recommendations

Write to ${CACHE}/aeo_summary.json as:
{
  "assessment": "...",
  "top_recommendations": ["...", "...", "..."]
}`,
    { label: 'aeo-summary', phase: 'Generate' }
  ),

])

// Merge title rewrites + schema blocks into a single rewrites.json
await agent(
  `Run this command:

cd ${ROOT} && uv run python -m modules.merge_rewrites ${CACHE} 2>&1

Report the merge stats.`,
  { label: 'merge-rewrites', phase: 'Generate' }
)

// ---------------------------------------------------------------------------
// Phase 5 — Report
// ---------------------------------------------------------------------------

phase('Report')

await agent(
  `You are producing a professional SEO + AEO technical audit report in Markdown.

Read ALL of the following files using the Read tool, then write the complete report:

- ${CACHE}/discovery.json   (URL count, page type breakdown)
- ${CACHE}/crawl.json        (status codes, render stats, SSR gaps — read only summary fields)
- ${CACHE}/technical.json    (all issues)
- ${CACHE}/metadata.json     (summary + first 20 rewrite_requests)
- ${CACHE}/schema.json       (summary + missing_by_type)
- ${CACHE}/eeat.json         (site_signals, gaps)
- ${CACHE}/eeat_summary.json (qualitative assessment)
- ${CACHE}/aeo.json          (summary)
- ${CACHE}/aeo_summary.json  (qualitative assessment)
- ${CACHE}/pagespeed.json    (summary + top_site_issues — may be skipped)
- ${CACHE}/rewrites.json     (generated titles, descriptions, schema blocks)

Write the report to ${outputFile}. Create the output directory if needed.

REPORT STRUCTURE (follow this order):

# SEO + AEO Technical Audit — [site domain]

## Executive Summary
Table: issue counts by severity (Critical / High / Medium / Low).
One paragraph of the 3 most important findings.

## 1. Server-Side Rendering
From technical.json ssr_gaps. If none: confirm SSR is healthy.

## 2. PageSpeed / Core Web Vitals
From pagespeed.json. If skipped, note it. Include mobile + desktop scores table.
List top failing audits with their titles.

## 3. Titles & Meta Descriptions
Table with columns: URL | Current Title | Recommended Title | Current Desc length | Issue
Use rewrites.json for recommended titles. Show all pages with issues.
Include the title + description templates.

## 4. E-E-A-T Signals
From eeat_summary.json assessment bullets.
Table of signals found vs missing.

## 5. AEO / Answer-Engine Readiness
From aeo_summary.json assessment bullets.
Key metrics table (question headings, answer chunk coverage, wall-of-text count, llms.txt).

## 6. Schema Markup
Table: page type → expected schemas → present → missing.
For each missing schema type, include a ready-to-use JSON-LD code block from rewrites.json.
If rewrites.json has no schema for that type, write the template with placeholders.

## 7. Technical Issues
Sub-sections: Broken Links | Canonical Tags | Redirects | Large Assets | Missing Tags.
Use tables where there are multiple items.

## 8. Prioritised Recommendations
Table: Priority | Issue | Pages Affected | Effort | Impact
Critical = fixes that directly block indexing or lose ranking.
High = significant SEO/AEO impact.
Medium = quality improvements.
Low = nice-to-have.

---
*Generated by SEO Audit Agent*

WRITING RULES:
- Use real data from the JSON files — no invented numbers
- Every table must have real rows from the data, not placeholder text
- For schema code blocks: use the actual jsonld from rewrites.json where available
- Keep narrative tight — one paragraph per section intro, then tables/bullets
- Note any unavailable sections (e.g. PageSpeed skipped) explicitly`,
  { label: 'report-writer', phase: 'Report' }
)

log(`Audit complete → ${outputFile}`)
