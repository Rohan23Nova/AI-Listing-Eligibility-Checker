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
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, field_validator

from backend.checkers.access_checker import run_access_checker
from backend.checkers.structure_checker import run_structure_checker
from backend.history_store import init_db, save_report, get_history, get_all_urls
from backend.report_generator import generate_report, report_to_dict
from backend.scoring_engine import compute_score


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

    Runs all deterministic checks (access, structure) in parallel.
    Content and empirical checks are stubs in Checkpoint 1 — they will
    be wired in M5/M6.
    """
    url = request.url
    start_ms = time.monotonic() * 1000

    try:
        # Run access and structure checks in parallel
        access_result, structure_result = await asyncio.gather(
            run_access_checker(url),
            run_structure_checker(url),
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Analysis failed for {url}: {str(e)}",
        )

    # Score
    score = compute_score(
        access=access_result,
        structure=structure_result,
        content=None,    # wired in M5
        empirical=None,  # wired in M6
    )

    duration_ms = int(time.monotonic() * 1000 - start_ms)

    # Generate report
    report = generate_report(
        url=url,
        access=access_result,
        structure=structure_result,
        score=score,
        content=None,
        empirical=None,
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
