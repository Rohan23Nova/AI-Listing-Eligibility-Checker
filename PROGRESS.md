# PROGRESS.md — Checkpoint 1

## Status: ✅ COMPLETE

All four milestones (M1–M4) implemented and verified.
**58/58 unit tests passing. Real URL output below.**

---

## M1 — Repo Scaffold ✅

Files created:
- `backend/` package with all sub-modules
- `backend/checkers/` sub-package
- `requirements.txt`, `pyproject.toml` (ruff + pytest config)
- `backend/config.py` — all env vars in one place, feature flags
- `backend/main.py` — FastAPI skeleton with `/analyze`, `/history`, `/health`
- `backend/models.py` — all shared dataclasses (BotSpec, AccessResult, StructureResult, Report, etc.)
- `tests/conftest.py`

---

## M2 — access_checker ✅

Implemented:
- `parse_robots_txt()` — stdlib `urllib.robotparser`, training/search bot split, Sitemap: directive extraction
- `check_sitemap()` — robots.txt directive priority → /sitemap.xml fallback
- `check_llms_txt()` — bonus-only, clearly labeled with caveat note
- `tier1_ua_fetch_matrix()` — concurrent async UA-spoof fetch for all 11 bots
- `run_access_checker()` — fans out all checks in parallel

Unit tests: 16 tests covering all-allow, all-deny, training/search split, 404→default-allow, network error→default-allow, sitemap directive, disallowed paths, UA blocking detection, network error vs blocked distinction, full integration.

---

## M3 — structure_checker ✅

Implemented:
- `extract_json_ld()` — `<script type="application/ld+json">`, handles single/list/@graph, malformed detection
- `extract_meta_tags()` — title, description, OG tags, canonical, robots meta (noindex, noarchive)
- `analyze_heading_hierarchy()` — H1 presence, count, logical H1→H2→H3 ordering
- `check_canonical()` — self-referential vs external canonical

Unit tests: 21 tests covering all schema formats, malformed JSON-LD, noindex/noarchive detection, heading hierarchy issues, canonical variants.

---

## M4 — scoring_engine v1 + report_generator v1 ✅

Scoring weights (single source of truth in `scoring_engine.py`):

| Component | Points |
|---|---|
| Search/Answer bots allowed (robots.txt) | 15 |
| Training bots allowed (robots.txt) | 5 |
| No UA-based HTTP blocking (empirical) | 10 |
| sitemap.xml present | 4 |
| llms.txt present (bonus only) | 1 |
| JSON-LD structured data | 12 |
| Meta description | 8 |
| OpenGraph tags | 5 |
| H1 heading present | 5 |
| Heading hierarchy | 5 |
| LLM content quality (stub) | 15 |
| LLM citation likelihood (stub) | 15 |
| **Total** | **100** |

Unit tests: 21 tests covering perfect score, near-zero, partial access splits, noindex override, LLM stub labeling, issue sorting, verdict thresholds, raw evidence presence.

---

## Real URL Verification (M4)

Tested 4 real, varied URLs. Run: `PYTHONPATH=. .venv/bin/python scripts/verify_m4.py`

### Results Summary

| Score | Verdict | URL |
|---|---|---|
| 38.6 | Poor | https://openai.com |
| 65.0 | Good | https://nytimes.com |
| 60.0 | Fair | https://en.wikipedia.org/wiki/Artificial_intelligence |
| 47.1 | Fair | https://example.com |

---

### openai.com (38.6 — Poor)

Notable findings:
- ✅ robots.txt: ALL bots allowed (6/6 training, 5/5 search)
- ❌ UA-level: All 11 bots receive errors/403 — openai.com blocks bot UAs at the HTTP layer despite allowing them in robots.txt. This is the training/search split in action empirically.
- ❌ No sitemap.xml at root
- ❌ No JSON-LD, no meta description, no OG tags on homepage
- Fix-pack: robots.txt additions (not needed), json_ld_snippet.html, meta_description.html

---

### nytimes.com (65.0 — Good)

Notable findings (the most interesting result — classic "blocks training, allows some search"):
- ✅ JSON-LD: `WebSite`, `NewsMediaOrganization` — excellent structured data
- ✅ sitemap.xml: Found via robots.txt Sitemap: directive
- ✅ Meta description, OG title, OG description, H1 — all present
- ❌ robots.txt: Blocks GPTBot, ClaudeBot, CCBot, Diffbot (training), OAI-SearchBot, Claude-SearchBot, PerplexityBot (search) — only Googlebot and Applebot-Extended allowed among search bots
- ❌ UA-level: GPTBot, ClaudeBot, CCBot, Amazonbot, Diffbot, OAI-SearchBot, Claude-SearchBot, PerplexityBot all return 403 empirically
- 🔑 Key demo: Shows training-block/search-partial split perfectly

Per-bot table:
```
GPTBot           training   BLOCK    403  ⚠ BLOCKED
ClaudeBot        training   BLOCK    403  ⚠ BLOCKED
Google-Extended  training   BLOCK    200  ok
CCBot            training   BLOCK    403  ⚠ BLOCKED
Amazonbot        training   Allow    403  ⚠ BLOCKED
Diffbot          training   BLOCK    403  ⚠ BLOCKED
OAI-SearchBot    search     BLOCK    403  ⚠ BLOCKED
Claude-SearchBot search     BLOCK    403  ⚠ BLOCKED
PerplexityBot    search     BLOCK    403  ⚠ BLOCKED
Applebot-Extended search    Allow    200  ok
Googlebot        search     Allow    200  ok
```

---

### en.wikipedia.org/wiki/Artificial_intelligence (60.0 — Fair)

Notable findings:
- ✅ robots.txt: ALL bots allowed (6/6 training, 5/5 search)
- ✅ JSON-LD: `Article` schema
- ✅ H1 present
- ❌ UA-level: Wikipedia returns 403 to all bot UAs — WAF/Cloudflare blocking at HTTP level despite open robots.txt. Empirical testing catches this correctly.
- ❌ No meta description
- ❌ No sitemap.xml found at root (Wikipedia uses subdomain-based sitemaps)
- ❌ Only partial OG tags (og:title present but not og:description)

---

### example.com (47.1 — Fair)

Notable findings:
- ✅ robots.txt: ALL bots allowed (minimal site, no robots.txt → default allow)
- ✅ UA-level: 200 OK for all bots (completely open)
- ❌ No sitemap, no JSON-LD, no meta description, no OG tags
- Fix-pack: json_ld_snippet.html, llms.txt template, meta_description.html

---

## Key Findings & Design Observations

1. **UA-spoof testing adds real value**: openai.com and wikipedia.org both show the gap between robots.txt (allow all) and HTTP reality (block all bot UAs). This is exactly why Tier-1 empirical testing matters.

2. **nytimes.com is the perfect demo URL**: It blocks training bots in robots.txt AND at the HTTP layer, blocks most search bots (Perplexity, Claude Search, OAI-Search), but allows Googlebot and Applebot-Extended. This shows the training/search split precisely.

3. **LLM scoring unavailability is clearly labeled**: All LLM items show "LLM scoring unavailable — GROQ_API_KEY or GEMINI_API_KEY not configured, or ENABLE_LLM=false" — no silent failures.

4. **Raw evidence is complete**: Every score item has its backing data. No black-box outputs.

---

## Tests

```
58 passed in 0.75s
```

All passing. Zero network calls in test suite (respx mocks).

---

## Checkpoint 1 Tag

Committed: `checkpoint-1` (see git log)
