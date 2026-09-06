# AI Listing Eligibility Checker

> **Can AI systems find, understand, and cite your website?**  
> An empirical, explainable eligibility report for ChatGPT, Claude, Perplexity, Google AI Overviews, and more.

[![Tests](https://img.shields.io/badge/tests-108%20passing-brightgreen)](#)
[![Python](https://img.shields.io/badge/python-3.12-blue)](#)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#)

---

## What it does

Paste any URL and get a scored report (0–100) showing:

| Signal | Method |
|---|---|
| **robots.txt** — per-bot allow/deny | Deterministic parser |
| **Training vs Search/Answer bot split** | 11 bots, 2 categories, 1 table |
| **UA-spoof empirical test** | Actual HTTP fetch with each bot's User-Agent |
| **JavaScript invisibility heuristic** | Word count + bundle pattern detection |
| **Playwright escalation** | Headless render for SPAs (conditional) |
| **Geo trip-wire** | Cloudflare Workers Tier-2 check (conditional) |
| **JSON-LD / structured data** | BeautifulSoup + lxml parser |
| **Meta description, OG tags, headings** | Deterministic HTML extraction |
| **Content quality** | LLM call #1 (Groq/Gemini, cached, fallback) |
| **Citation likelihood** | LLM call #2 (Groq/Gemini, cached, fallback) |
| **Score history** | SQLite — re-run after fixes to see improvement |
| **Fix-pack download** | robots.txt / JSON-LD / meta templates |

### Training vs Search/Answer bots — why it matters

| Category | Bots | Effect of blocking |
|---|---|---|
| 🎓 **Training** | GPTBot, ClaudeBot, Google-Extended, CCBot, Amazonbot, Diffbot | Removes your content from future LLM training data |
| 🔍 **Search/Answer** | OAI-SearchBot, Claude-SearchBot, PerplexityBot, Applebot-Extended, Googlebot | Removes your content from AI-powered search results |

**Blocking training bots is a legitimate business choice** and does not affect AI search visibility.  
**Blocking search/answer bots removes you from AI answer engines** — this is what the score penalises most.

---

## Quick Start

### Requirements
- Python 3.12+
- Free API key from [Groq](https://console.groq.com/) (or Gemini) — optional for rule-based scoring

### Setup

```bash
git clone https://github.com/Rohan23Nova/AI-Listing-Eligibility-Checker.git
cd "AI-Listing-Eligibility-Checker"

# Create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# (Optional) Install Playwright browser for JS-heavy sites
playwright install chromium

# Configure environment
cp .env.example .env
# Edit .env — at minimum set GROQ_API_KEY if you want LLM scoring
```

### Run

```bash
# Start the backend (serves frontend at http://localhost:8000/)
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Open in browser
open http://localhost:8000/
```

### API usage

```bash
# Analyze a URL
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# Get history for a URL
curl "http://localhost:8000/history/https%3A%2F%2Fexample.com"

# List all analyzed URLs
curl http://localhost:8000/history
```

---

## Architecture

```
backend/
├── main.py                  # FastAPI app, /analyze /history /health
├── config.py                # All env vars, feature flags, timeouts
├── models.py                # Shared dataclasses — BotSpec, AccessResult, Report, etc.
├── llm_client.py            # Centralized LLM abstraction — caching, Groq/Gemini, mock fallback
├── scoring_engine.py        # Weighted scoring — all weights in one block, every point explainable
├── report_generator.py      # Report assembly + fix-pack generator
├── history_store.py         # SQLite history (aiosqlite)
└── checkers/
    ├── access_checker.py    # robots.txt, sitemap, llms.txt, Tier-1 UA-spoof matrix
    ├── structure_checker.py # JSON-LD, meta tags, heading hierarchy, canonical
    ├── content_checker.py   # Word count, SPA heuristic, Playwright escalation, LLM call #1
    └── empirical_tester.py  # Tier-1/2/3 anomaly detection, geo trip-wire, LLM call #2

frontend/
└── index.html               # Single-file SPA — Tailwind CDN, no build step

workers/
└── geo_fetch/               # Cloudflare Worker — geo trip-wire (deploy with Wrangler)
    ├── index.js
    └── wrangler.toml

tests/                       # 108 tests — zero network calls (respx + unittest.mock)
```

### Tiered escalation design

All expensive checks are **conditional** — cheap checks run always, expensive ones only escalate when evidence demands it:

```
Tier-1 (always, ~200ms):   Plain httpx UA-spoof fetch for all 11 bots
Tier-2 (if anomaly, ~3s):  Cloudflare Worker geo trip-wire — one probe bot, 2-3 regions
Tier-3 (if geo diff, ~8s): Full bot×region matrix — only if Tier-2 confirms geo anomaly

Playwright (if SPA, ~5s):  Headless render — only if word_count < 100 AND JS bundles detected
LLM (if enabled, ~2s):     2 calls (content quality + citation), both cached in SQLite
```

---

## Scoring

| Category | Max | Notes |
|---|---|---|
| Search/answer bots in robots.txt | 15 | Per-bot proportional |
| Training bots in robots.txt | 5 | Owner's choice — not a penalty |
| No UA-based blocking (empirical) | 10 | Proportional across 11 bots |
| sitemap.xml | 4 | — |
| llms.txt | 1 | **Bonus-only** — no confirmed adoption, see note |
| JSON-LD structured data | 12 | Partial credit for malformed |
| Meta description | 8 | Partial credit for very short |
| OpenGraph tags | 5 | — |
| H1 heading | 5 | Partial credit for multiple H1s |
| Heading hierarchy | 5 | Deductions per issue |
| Content quality (LLM) | 15 | Unavailable → score scaled to 70 |
| Citation likelihood (LLM) | 15 | Unavailable → score scaled to 70 |
| **Total** | **100** | |

> **Note on llms.txt**: This is a community proposal with no standards-body backing and no confirmed adoption by any major AI provider as of 2025. It's a 1-point bonus only and never a pass/fail gate.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key (free tier, preferred for LLM calls) |
| `GEMINI_API_KEY` | — | Gemini API key (free tier, fallback) |
| `ENABLE_LLM` | `false` | Enable LLM scoring (set `true` with an API key) |
| `ENABLE_PLAYWRIGHT` | `false` | Enable Playwright SPA escalation |
| `ENABLE_GEO_CHECK` | `false` | Enable Cloudflare Worker geo trip-wire |
| `GEO_WORKER_URL_US` | — | US Cloudflare Worker URL |
| `GEO_WORKER_URL_EU` | — | EU Cloudflare Worker URL |
| `GEO_WORKER_URL_APAC` | — | APAC Cloudflare Worker URL |
| `SQLITE_DB_PATH` | `data/history.db` | SQLite DB path |
| `LLM_MODEL` | `llama-3.1-8b-instant` | Groq model name |

---

## Deploying the Cloudflare Worker (Tier-2 geo check)

```bash
cd workers/geo_fetch
npm install -g wrangler
wrangler login

# Deploy 2-3 instances in different regions
wrangler deploy --name geo-fetch-us
wrangler deploy --name geo-fetch-eu
wrangler deploy --name geo-fetch-apac

# Copy the deployment URLs into .env as GEO_WORKER_URL_US etc.
```

---

## Tests

```bash
pytest tests/ -v
# 108 tests, ~1s, zero network calls
```

Test coverage:
- `test_access_checker.py` — 16 tests (robots.txt, sitemap, llms.txt, UA-spoof)
- `test_structure_checker.py` — 20 tests (JSON-LD, meta, headings, canonical)
- `test_scoring_engine.py` — 21 tests (weights, verdicts, issues sorting)
- `test_content_checker.py` — 20 tests (SPA heuristic, Playwright paths, LLM stub)
- `test_empirical_tester.py` — 13 tests (tier escalation, geo anomaly, LLM stub)
- `test_llm_client.py` — 18 tests (caching, mock fallback, API error handling)
