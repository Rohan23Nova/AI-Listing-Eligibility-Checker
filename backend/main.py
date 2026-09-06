"""
main.py — FastAPI application entry point.

Endpoints:
  POST /analyze          Analyze a URL, return full report
  GET  /history/{url}    Get score history for a URL
  GET  /history          Get all analyzed URLs with latest scores
  GET  /health           Health check
"""

from __future__ import annotations

import time
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

from backend.checkers.access_checker import run_access_checker
from backend.checkers.structure_checker import run_structure_checker
from backend.checkers.content_checker import run_content_checker
from backend.checkers.empirical_tester import run_empirical_tester
from backend.history_store import init_db, save_report, get_history, get_all_urls
from backend.report_generator import generate_report, report_to_dict
from backend.scoring_engine import compute_score
from backend.models import SEARCH_BOTS


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB on startup."""
    await init_db()
    yield


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AI Listing Eligibility Checker",
    description=(
        "Analyzes whether a website can be crawled, understood, and cited by "
        "AI systems (ChatGPT, Claude, Perplexity, Google AI Overviews, etc.)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend from /app — mount after API routes are registered
# so /analyze and /history routes take precedence.
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the frontend SPA."""
    index_path = os.path.join(_FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<p>Frontend not found. Run from project root.</p>", status_code=404)


# ── Request / Response schemas ────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(request: AnalyzeRequest) -> JSONResponse:
    """
    Full AI eligibility analysis for a URL.

    Fan-out pattern: access and structure checkers run in parallel (both need
    the same page, but fetch independently). Content checker reuses the access
    fetch result if available. Empirical tester uses access_checker's Tier-1
    UA results directly (no duplicate network calls).
    """
    url = request.url
    start_ms = time.monotonic() * 1000

    try:
        # Phase 1: access + structure in parallel (both cheap, deterministic)
        access_result, structure_result = await asyncio.gather(
            run_access_checker(url),
            run_structure_checker(url),
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Analysis failed for {url}: {str(e)}",
        )

    try:
        # Phase 2: content_checker + empirical_tester in parallel
        # content_checker fetches independently (may use Playwright)
        # empirical_tester re-uses access_checker's Tier-1 UA results (no dup network)
        search_bots_allowed = sum(
            1 for r in access_result.robots_per_bot
            if r.bot in SEARCH_BOTS and r.allowed
        )
        content_result, empirical_result = await asyncio.gather(
            run_content_checker(url),
            run_empirical_tester(
                url=url,
                ua_results=access_result.ua_spoof_results,
                meta_description=structure_result.meta.description,
                json_ld_types=structure_result.json_ld.types,
                word_count=0,  # updated below after content check
                search_bots_allowed=search_bots_allowed,
                search_bots_total=len(SEARCH_BOTS),
            ),
        )
    except Exception as e:
        # Phase 2 failures are non-fatal — degrade gracefully
        content_result = None
        empirical_result = None

    duration_ms = int(time.monotonic() * 1000 - start_ms)

    # Score with all available results
    score = compute_score(
        access=access_result,
        structure=structure_result,
        content=content_result,
        empirical=empirical_result,
    )

    # Generate report
    report = generate_report(
        url=url,
        access=access_result,
        structure=structure_result,
        score=score,
        content=content_result,
        empirical=empirical_result,
        duration_ms=duration_ms,
    )

    report_dict = report_to_dict(report)

    # Persist to history
    try:
        await save_report(url, score.total, score.verdict, report_dict)
    except Exception:
        pass  # History persistence failure must not break the analysis response

    return JSONResponse(content=report_dict)


@app.get("/history")
async def history_all() -> JSONResponse:
    """Return all analyzed URLs with their latest scores."""
    urls = await get_all_urls()
    return JSONResponse(content={"urls": urls})


@app.get("/history/{url:path}")
async def history_for_url(url: str) -> JSONResponse:
    """Return score history for a specific URL."""
    decoded_url = unquote(url)
    if not decoded_url.startswith(("http://", "https://")):
        decoded_url = "https://" + decoded_url
    rows = await get_history(decoded_url)
    return JSONResponse(content={"url": decoded_url, "history": rows})


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    from backend.config import HOST, PORT
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=True)
