# PROGRESS.md — All Checkpoints

---

## Checkpoint 1 ✅ — M1–M4: Scaffold + Core Checkers

**Tag:** `checkpoint-1` | **Tests:** 58/58

### M1 — Scaffold
Full folder structure, `requirements.txt`, `pyproject.toml`, `backend/config.py`,
`backend/models.py` (all shared dataclasses, 11 BotSpecs, BotCategory enum),
`backend/main.py` FastAPI skeleton.

### M2 — access_checker
- `parse_robots_txt()` — stdlib robotparser, training/search split, Sitemap: extraction
- `check_sitemap()` — robots.txt directive → /sitemap.xml fallback
- `check_llms_txt()` — bonus-only with explicit caveat note
- `tier1_ua_fetch_matrix()` — concurrent async UA-spoof for all 11 bots
- 16 unit tests — all pass

### M3 — structure_checker
- `extract_json_ld()` — single/list/@graph formats, malformed detection
- `extract_meta_tags()` — title, desc, OG, canonical, robots meta (noindex/noarchive)
- `analyze_heading_hierarchy()` — H1 count, H1→H2→H3 ordering
- `check_canonical()` — self-ref vs external
- 21 unit tests — all pass

### M4 — scoring_engine + report_generator v1
Weights (single source of truth in `scoring_engine.py`):

| Component | Points |
|---|---|
| Search/answer bots (robots.txt) | 15 |
| Training bots (robots.txt) | 5 |
| UA-based HTTP blocking (empirical) | 10 |
| sitemap.xml | 4 |
| llms.txt (bonus only) | 1 |
| JSON-LD | 12 |
| Meta description | 8 |
| OG tags | 5 |
| H1 heading | 5 |
| Heading hierarchy | 5 |
| LLM content quality | 15 |
| LLM citation likelihood | 15 |
| **Total** | **100** |

Real URL verification:

| Score | Verdict | URL |
|---|---|---|
| 38.6 | Poor | openai.com — allows all bots in robots.txt, blocks all via WAF |
| 65.0 | Good | nytimes.com — perfect training/search split demo URL |
| 60.0 | Fair | en.wikipedia.org — open robots.txt, but blocks all bot UAs via Cloudflare |
| 47.1 | Fair | example.com — open, no structured data |

---

## Checkpoint 2 ✅ — M5–M7: Content + Empirical + Frontend

**Tag:** `checkpoint-2` | **Tests:** 90/90

### M5 — content_checker
Tiered content analysis:
- **Tier-1 (always):** Plain httpx fetch → visible text extraction → word count
- **Heuristic:** `word_count < 100 AND JS bundles detected` → `likely_spa=True`
- **Tier-2 (conditional):** `likely_spa=True AND ENABLE_PLAYWRIGHT=true` → headless Chromium render
- JS bundle patterns: `.chunk.js`, `bundle.js`, `_next/static`, `react-dom`, `vendor.*.js`, Next.js, Nuxt.js
- 20 unit tests covering both heuristic-only and Playwright escalation paths

### M6 — empirical_tester
3-tier escalation:
- **Tier-1 (always):** Reuses access_checker UA results, detects training/search differential
- **Tier-2 (anomaly only):** Cloudflare Worker geo probe — one probe bot, multiple regions
- **Tier-3 (geo-diff only):** Full bot×region matrix
- Cloudflare Worker at `workers/geo_fetch/index.js` + `wrangler.toml`
- 13 unit tests covering all tier paths

### M7 — Frontend SPA
`frontend/index.html` — single file, Tailwind CDN, zero build step:
- Animated score ring (SVG + stroke-dashoffset animation)
- Per-bot table with **🎓 Training** / **🔍 Search/Answer** sections
- Score breakdown bars, prioritized issues with severity badges
- Collapsible raw evidence panel
- Fix-pack download buttons
- History panel (toggleable global list)
- Backend serves frontend at `GET /` (same-origin API calls, no CORS needed)

End-to-end verification: nytimes.com → 65.0 Good, 695 words, likely_spa=False, Tier-1 anomaly=True, LLM=unavailable (no key)

---

## Checkpoint 3 ✅ — M8–M9: LLM Wiring + History UI

**Tag:** `checkpoint-3` | **Tests:** 108/108

### M8 — Centralized LLM client (`backend/llm_client.py`)
- **Caching:** Both calls cached in SQLite (`llm_cache` table, 24hr TTL, keyed by `sha256(url + content)`)
- **Providers:** Groq (primary, fast free tier) → Gemini (fallback) → mock auto-degradation
- **Graceful fallback:** Any exception → `"unavailable"` with error text, score scaled to 70 base, never crashes
- **Mock explicitly tested** (spec requirement): 4 test cases cover disabled flag, missing key, API exception, cache hit suppressing API call
- Replaced duplicate inline LLM code in both `content_checker.py` and `empirical_tester.py`
- 18 new tests — all pass

### M9 — History timeline + share
- **Score history sparkline** — SVG polyline, appears after second analysis of any URL
- **Delta display** — `+/- pts since first run` in green/red
- **Share button** — copies `#url=...` deep link, clipboard toast
- **LLM active/inactive badge** — purple "active" vs amber "unavailable" based on real provider
- URL hash auto-fills and auto-runs: `/?#url=https://example.com`

README first draft written.

---

## Checkpoint 4 ✅ — M10–M11: Deploy + Demo + Polish

**Tag:** `checkpoint-4` | **Tests:** 108/108

### M10-A — Pre-deploy hardening
- `Procfile` — `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- `render.yaml` — IaC blueprint for one-click Render deploy (free tier, Python 3.12, `/tmp/history.db`)
- `aiofiles==24.1.0` added to `requirements.txt` (needed by FastAPI static serving)
- Frontend `<meta name="api-url">` for configurable backend URL in split-origin deploys
- OG meta tags added to frontend (the tool itself passes its own checks)

### M10-B/C — Deploy to Render
One-click deploy URL:
```
https://render.com/deploy?repo=https://github.com/Rohan23Nova/AI-Listing-Eligibility-Checker
```
Single Render web service serves both backend (FastAPI) and frontend (`GET /` → `index.html`).
Set `GROQ_API_KEY` + `ENABLE_LLM=true` in dashboard after deploy for full 100-point scoring.

### M10-D — Cloudflare Worker
```bash
cd workers/geo_fetch
wrangler login && wrangler deploy --name geo-fetch-us
# Set GEO_WORKER_URL_US in Render env + ENABLE_GEO_CHECK=true
```

### M11-A — Before/after demo
`example.com` baseline:
- Score: ~47 Fair — fully open robots.txt + UA access, but zero structured data
- Issues: no JSON-LD (-12), no meta description (-8), no OG tags (-5), no sitemap (-4)
- Fix-pack: `json_ld_snippet.html`, `meta_description.html`, `llms.txt` template

After applying JSON-LD + meta desc + OG (simulated):
- Score: ~69 Good → **+22 pts**
- History sparkline shows the delta immediately in UI

### M11-B — Final README
- "Deploy to Render" button badge
- Architecture Mermaid diagram
- Before/after demo table
- Complete scoring breakdown
- All design constraints documented

---

## Design principles (non-negotiable — verified throughout)

1. ✅ **Zero paid infrastructure** — Render free, Cloudflare Workers free, Groq/Gemini free tiers
2. ✅ **Deterministic checks are plain code** — LLM calls exactly 2, both cached, both with graceful mock fallback
3. ✅ **Every score is explainable** — `ScoreBreakdownItem` carries raw evidence for every point
4. ✅ **Training vs search/answer bots distinguished everywhere** — `BotCategory` enum, per-bot table, score breakdown, UI labels
5. ✅ **llms.txt bonus-only** — 1 pt, never gate, UI copy says "no confirmed provider adoption"
6. ✅ **Tiered/adaptive depth** — cheap checks always, expensive only on evidence; documented in code comments

---

## Test count progression

| Checkpoint | Tests |
|---|---|
| 1 | 58 |
| 2 | 90 |
| 3 | 108 |
| 4 | 108 (no regressions) |
