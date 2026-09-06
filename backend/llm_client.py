"""
llm_client.py — Centralized LLM abstraction with caching and mock fallback.

Design principles (per project spec):
  1. CACHING: Both LLM calls are cached keyed by (call_type, content_hash) in
     a lightweight SQLite table. This avoids re-billing identical content on
     repeated analyses of the same URL, and keeps the demo fast.

  2. FEATURE FLAGS: All LLM calls are gated behind ENABLE_LLM and the resolved
     provider. If GROQ_API_KEY / GEMINI_API_KEY is unset, EFFECTIVE_LLM_PROVIDER
     resolves to "mock" automatically (see config.py).

  3. GRACEFUL FALLBACK: Any exception from the API (network, quota, rate-limit,
     auth error) is caught and returns a clearly-labeled MockResult — the
     analysis never hangs or crashes because of an LLM call. The mock label is
     always propagated to the UI so users know the score is partial.

  4. CALL TYPES: Exactly two LLM calls are made per analysis:
       - "content_quality":    Is this page's content readable, substantive,
                               citable? (drives LLM scoring in scoring_engine)
       - "citation_likelihood": Would AI answer engines actually cite this URL?
                               (drives LLM scoring in scoring_engine)
     Both calls use the same timeout, the same cache, and the same fallback logic.

  5. PROVIDER PRIORITY: Groq (fast free tier) → Gemini (fallback) → mock.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal

import aiosqlite
import httpx

from backend.config import (
    ENABLE_LLM,
    EFFECTIVE_LLM_PROVIDER,
    GROQ_API_KEY,
    GEMINI_API_KEY,
    LLM_MODEL,
    SQLITE_DB_PATH,
)

# ── Constants ─────────────────────────────────────────────────────────────────

LLM_CALL_TIMEOUT = 30.0          # seconds — generous but bounded
LLM_CACHE_TTL_HOURS = 24         # cached responses expire after 24 hours
LLM_MAX_PROMPT_CHARS = 7_000     # cap prompt size before sending to API

CallType = Literal["content_quality", "citation_likelihood"]

MOCK_LABEL = "unavailable"
MOCK_REASON_NO_KEY = (
    "LLM scoring unavailable — no API key configured. "
    "Set GROQ_API_KEY or GEMINI_API_KEY and ENABLE_LLM=true to enable."
)
MOCK_REASON_DISABLED = (
    "LLM scoring unavailable — ENABLE_LLM=false. "
    "Set ENABLE_LLM=true to enable."
)
MOCK_REASON_ERROR = "LLM scoring unavailable — API call failed: {error}"


@dataclass
class LLMResult:
    """Structured result from a single LLM call."""
    score: float | None         # 0.0–1.0, None if unavailable
    label: str                  # "high" | "medium" | "low" | "unavailable"
    reasoning: str | None       # One-sentence explanation
    provider: str               # "groq" | "gemini" | "mock"
    cached: bool = False        # True if result came from cache
    error: str | None = None    # Error message if call failed


# ── Cache ─────────────────────────────────────────────────────────────────────

async def _ensure_cache_table() -> None:
    """Create the LLM cache table if it doesn't exist."""
    import os
    from pathlib import Path
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(SQLITE_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key   TEXT PRIMARY KEY,
                call_type   TEXT NOT NULL,
                score       REAL,
                label       TEXT NOT NULL,
                reasoning   TEXT,
                provider    TEXT NOT NULL,
                created_at  REAL NOT NULL
            )
        """)
        await db.commit()


def _cache_key(call_type: CallType, content: str) -> str:
    """Deterministic cache key = sha256 of (call_type + content)."""
    raw = f"{call_type}:{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _cache_get(key: str) -> LLMResult | None:
    """Return a cached LLMResult if present and not expired."""
    cutoff = time.time() - LLM_CACHE_TTL_HOURS * 3600
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM llm_cache WHERE cache_key=? AND created_at>?",
                (key, cutoff),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return LLMResult(
                        score=row["score"],
                        label=row["label"],
                        reasoning=row["reasoning"],
                        provider=row["provider"],
                        cached=True,
                    )
    except Exception:
        pass  # Cache miss is always safe
    return None


async def _cache_set(key: str, call_type: CallType, result: LLMResult) -> None:
    """Write an LLMResult to the cache."""
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO llm_cache
                    (cache_key, call_type, score, label, reasoning, provider, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, call_type, result.score, result.label, result.reasoning,
                 result.provider, time.time()),
            )
            await db.commit()
    except Exception:
        pass  # Cache write failure is always safe


# ── Prompt builders ───────────────────────────────────────────────────────────

def _content_quality_prompt(url: str, text_excerpt: str) -> str:
    return f"""You are evaluating whether a web page's content is suitable for AI systems to cite.
Rate the following page excerpt on three dimensions, each from 1 (very poor) to 5 (excellent):

1. READABILITY: Is the text clear, well-structured prose (not keyword stuffing or boilerplate)?
2. SUBSTANTIVENESS: Does it contain real, specific information (not thin/vague content)?
3. CITABILITY: Is it factual, attributable, and the kind of content AI systems would cite?

Page URL: {url}

Excerpt:
---
{text_excerpt[:LLM_MAX_PROMPT_CHARS]}
---

Reply in this exact format (no other text):
READABILITY: <1-5>
SUBSTANTIVENESS: <1-5>
CITABILITY: <1-5>
REASONING: <one sentence explaining the scores>"""


def _citation_likelihood_prompt(
    url: str,
    meta_description: str | None,
    json_ld_types: list[str],
    word_count: int,
    search_bots_allowed: int,
    search_bots_total: int,
) -> str:
    return f"""You are assessing how likely an AI answer engine (like ChatGPT, Perplexity, or Google AI Overviews) would be to cite this web page in its responses.

Rate citation likelihood from 1 (very unlikely) to 5 (very likely) based on these signals:
- Authoritative domain and content type
- Clear metadata that identifies what the page is about
- Accessibility to AI search crawlers

Page signals:
URL: {url}
Meta description: {meta_description or '(none)'}
Structured data types: {', '.join(json_ld_types) if json_ld_types else '(none)'}
Content word count: {word_count}
Search/answer bots allowed (robots.txt): {search_bots_allowed}/{search_bots_total}

Reply in this exact format (no other text):
CITATION_LIKELIHOOD: <1-5>
REASONING: <one sentence>"""


# ── Response parsers ──────────────────────────────────────────────────────────

def _parse_quality_response(raw: str) -> tuple[float, str, str]:
    """Parse a content_quality LLM response into (score, label, reasoning)."""
    import re
    scores: list[int] = []
    reasoning = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        for prefix in ("READABILITY:", "SUBSTANTIVENESS:", "CITABILITY:"):
            if line.startswith(prefix):
                m = re.search(r"[1-5]", line)
                if m:
                    scores.append(int(m.group()))
        if line.startswith("REASONING:"):
            reasoning = line[len("REASONING:"):].strip()

    if not scores:
        return 0.5, "medium", "Could not parse LLM response"

    avg = sum(scores) / len(scores)        # 1–5
    normalized = round((avg - 1) / 4, 3)  # 0.0–1.0
    label = "high" if normalized >= 0.75 else ("medium" if normalized >= 0.45 else "low")
    return normalized, label, reasoning or "No reasoning provided"


def _parse_citation_response(raw: str) -> tuple[float, str, str]:
    """Parse a citation_likelihood LLM response into (score, label, reasoning)."""
    import re
    score_raw = 3
    reasoning = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("CITATION_LIKELIHOOD:"):
            m = re.search(r"[1-5]", line)
            if m:
                score_raw = int(m.group())
        elif line.startswith("REASONING:"):
            reasoning = line[len("REASONING:"):].strip()

    normalized = round((score_raw - 1) / 4, 3)
    label = "high" if normalized >= 0.75 else ("medium" if normalized >= 0.45 else "low")
    return normalized, label, reasoning or "No reasoning provided"


# ── API callers ───────────────────────────────────────────────────────────────

async def _call_groq(prompt: str, max_tokens: int = 200) -> str:
    """Call Groq chat completions API."""
    async with httpx.AsyncClient(timeout=LLM_CALL_TIMEOUT) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_gemini(prompt: str, max_tokens: int = 200) -> str:
    """Call Gemini generateContent API."""
    model = LLM_MODEL if "gemini" in LLM_MODEL.lower() else "gemini-1.5-flash-latest"
    async with httpx.AsyncClient(timeout=LLM_CALL_TIMEOUT) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
            },
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _mock_result(reason: str) -> LLMResult:
    return LLMResult(score=None, label=MOCK_LABEL, reasoning=reason, provider="mock")


# ── Main public API ───────────────────────────────────────────────────────────

async def llm_content_quality(url: str, text_excerpt: str) -> LLMResult:
    """
    LLM call #1: content quality judgment.

    Cached by sha256(url + text_excerpt[:500]).  The excerpt is hashed not
    stored, so cache keys remain small even for large pages.
    """
    if not ENABLE_LLM:
        return _mock_result(MOCK_REASON_DISABLED)
    if EFFECTIVE_LLM_PROVIDER == "mock":
        return _mock_result(MOCK_REASON_NO_KEY)

    await _ensure_cache_table()
    # Cache key uses url + first 500 chars of excerpt (stable for same page)
    key = _cache_key("content_quality", url + text_excerpt[:500])
    cached = await _cache_get(key)
    if cached:
        return cached

    prompt = _content_quality_prompt(url, text_excerpt)

    try:
        if EFFECTIVE_LLM_PROVIDER == "groq":
            raw = await _call_groq(prompt, max_tokens=200)
        elif EFFECTIVE_LLM_PROVIDER == "gemini":
            raw = await _call_gemini(prompt, max_tokens=200)
        else:
            return _mock_result(MOCK_REASON_NO_KEY)

        score, label, reasoning = _parse_quality_response(raw)
        result = LLMResult(score=score, label=label, reasoning=reasoning,
                           provider=EFFECTIVE_LLM_PROVIDER)
        await _cache_set(key, "content_quality", result)
        return result

    except Exception as e:
        # Graceful fallback — never crash because of an LLM call
        err = str(e)[:120]
        return _mock_result(MOCK_REASON_ERROR.format(error=err))


async def llm_citation_likelihood(
    url: str,
    meta_description: str | None,
    json_ld_types: list[str],
    word_count: int,
    search_bots_allowed: int,
    search_bots_total: int,
) -> LLMResult:
    """
    LLM call #2: citation likelihood judgment.

    Cached by sha256(url + meta_description + types).
    """
    if not ENABLE_LLM:
        return _mock_result(MOCK_REASON_DISABLED)
    if EFFECTIVE_LLM_PROVIDER == "mock":
        return _mock_result(MOCK_REASON_NO_KEY)

    await _ensure_cache_table()
    cache_content = f"{url}|{meta_description}|{','.join(json_ld_types)}|{word_count}"
    key = _cache_key("citation_likelihood", cache_content)
    cached = await _cache_get(key)
    if cached:
        return cached

    prompt = _citation_likelihood_prompt(
        url=url,
        meta_description=meta_description,
        json_ld_types=json_ld_types,
        word_count=word_count,
        search_bots_allowed=search_bots_allowed,
        search_bots_total=search_bots_total,
    )

    try:
        if EFFECTIVE_LLM_PROVIDER == "groq":
            raw = await _call_groq(prompt, max_tokens=150)
        elif EFFECTIVE_LLM_PROVIDER == "gemini":
            raw = await _call_gemini(prompt, max_tokens=150)
        else:
            return _mock_result(MOCK_REASON_NO_KEY)

        score, label, reasoning = _parse_citation_response(raw)
        result = LLMResult(score=score, label=label, reasoning=reasoning,
                           provider=EFFECTIVE_LLM_PROVIDER)
        await _cache_set(key, "citation_likelihood", result)
        return result

    except Exception as e:
        err = str(e)[:120]
        return _mock_result(MOCK_REASON_ERROR.format(error=err))
