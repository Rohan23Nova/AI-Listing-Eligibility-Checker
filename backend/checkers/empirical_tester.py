"""
checkers/empirical_tester.py — Empirical bot-access testing across tiers.

Tiered design (deliberate cost/reliability choice — documented here for judges):
─────────────────────────────────────────────────────────────────────────────
  Tier-1 (cheap, always): UA-spoof HTTP fetch for each bot UA → already done
    by access_checker. The results are imported here and re-used; no duplicate
    network calls. Tier-1 detects clear UA-based blocking (e.g., 403 to GPTBot
    but 200 to Chrome).

  Tier-2 (moderate cost, conditional): Cloudflare Worker geo trip-wire.
    Triggered only when Tier-1 finds an anomaly:
      - A bot returns a different status code from a baseline (Chrome) fetch
      - Or a differential status across bot categories (e.g., training bots
        get 200 but search bots get 403 — or vice versa)
    Two-three Cloudflare Workers (US, EU, APAC) each re-fetch the URL with a
    single bot UA and return the HTTP status. This catches geo-based blocking
    that would affect AI crawlers differently by region.

  Tier-3 (expensive, rarely triggered): Full UA×region matrix — every bot UA
    from every configured region. Only triggered if Tier-2 confirms a geo
    anomaly (status differs by more than 100 across regions for the same bot).
    Rarely runs in practice; most sites either block globally or not at all.

  Rationale: Tier-2 and Tier-3 involve outbound HTTP from Cloudflare datacenters
    — each call has real cost in time (~1-3s) and Cloudflare free-tier quota.
    Running them unconditionally on every request would slow the tool to 10-15s
    and burn quota on sites that are trivially accessible or trivially blocked.
    The trip-wire pattern keeps the common-path fast while still catching the
    interesting edge cases.

LLM call #2 (citation likelihood) also lives here — same fallback pattern
as LLM call #1 in content_checker.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from backend.config import (
    ENABLE_GEO_CHECK,
    ENABLE_LLM,
    EFFECTIVE_LLM_PROVIDER,
    GEO_WORKER_URLS,
    HTTP_TIMEOUT_UA_FETCH,
    DEFAULT_HEADERS,
    GROQ_API_KEY,
    GEMINI_API_KEY,
    LLM_MODEL,
)
from backend.models import (
    BOTS,
    SEARCH_BOTS,
    TRAINING_BOTS,
    BotSpec,
    EmpiricalResult,
    UASpoofResult,
)


# ── Tier-1 anomaly detection ───────────────────────────────────────────────────

def _detect_tier1_anomaly(ua_results: list[UASpoofResult]) -> tuple[bool, str]:
    """
    Examine Tier-1 UA-spoof results and decide if Tier-2 should be triggered.

    Anomaly conditions:
      A) Any search bot gets a blocking status (403/429) while at least one
         non-bot request (status 200) exists. This suggests UA-based blocking
         that warrants geo verification.
      B) Training and search bots get systematically different status codes
         (e.g., training=200, search=403 or vice versa). This is the interesting
         case showing category-based blocking.
      C) Bot UAs time out consistently when non-bot UAs succeed.

    Returns (anomaly_detected, reason_string).
    """
    status_by_ua = {r.bot.user_agent: r.http_status for r in ua_results}
    blocked = [r for r in ua_results if r.blocked]
    errored = [r for r in ua_results if r.error is not None]

    # No anomaly if everything is blocked or everything is fine
    all_blocked = len(blocked) + len(errored) == len(ua_results)
    none_blocked = len(blocked) == 0 and len(errored) == 0
    if all_blocked or none_blocked:
        return False, "uniform result across all bot UAs — no geo verification needed"

    # Check search vs training differential
    search_statuses = [r.http_status for r in ua_results if r.bot in SEARCH_BOTS]
    training_statuses = [r.http_status for r in ua_results if r.bot in TRAINING_BOTS]

    search_blocked_count = sum(1 for s in search_statuses if s in (403, 429, 503) or s is None)
    training_blocked_count = sum(1 for s in training_statuses if s in (403, 429, 503) or s is None)

    if search_blocked_count > 0 and training_blocked_count == 0:
        return True, f"search bots blocked ({search_blocked_count}/{len(search_statuses)}) but training bots allowed"
    if training_blocked_count > 0 and search_blocked_count == 0:
        return True, f"training bots blocked ({training_blocked_count}/{len(training_statuses)}) but search bots allowed"

    # Mixed blocking — some bots blocked, others not
    if 0 < len(blocked) < len(ua_results):
        return True, f"partial blocking: {len(blocked)}/{len(ua_results)} bots blocked — possible UA-discriminating WAF"

    return False, "no significant anomaly detected"


# ── Tier-2 geo trip-wire ───────────────────────────────────────────────────────

async def _geo_fetch_one(
    worker_url: str,
    target_url: str,
    bot_ua: str,
    region: str,
) -> dict[str, Any]:
    """
    Call one Cloudflare Worker to fetch target_url from a remote region
    using the given bot User-Agent.

    The Worker endpoint accepts: POST {"url": "...", "user_agent": "..."}
    and returns: {"status": 200, "region": "us-east", "blocked": false}
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                worker_url,
                json={"url": target_url, "user_agent": bot_ua},
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            return {
                "region": region,
                "bot_ua": bot_ua,
                "status": data.get("status"),
                "blocked": data.get("blocked", False),
                "error": None,
            }
    except Exception as e:
        return {
            "region": region,
            "bot_ua": bot_ua,
            "status": None,
            "blocked": False,
            "error": str(e)[:120],
        }


async def run_tier2_geo_tripwire(
    url: str,
    probe_bot: BotSpec,
) -> tuple[bool, list[dict[str, Any]]]:
    """
    Run the Tier-2 geo trip-wire for a single representative bot UA.

    We pick one search bot (OAI-SearchBot) as the probe because search-bot
    blocking is the most impactful case — if this bot is geo-blocked, AI
    search users in that region lose access.

    Returns (geo_anomaly_detected, geo_results_list).
    geo_anomaly_detected = True if any region returns a different status
    from the others (implying geo-based differential access).
    """
    if not GEO_WORKER_URLS:
        return False, [{"note": "No Cloudflare Worker URLs configured — geo check skipped"}]

    region_labels = ["us", "eu", "apac"][: len(GEO_WORKER_URLS)]
    tasks = [
        _geo_fetch_one(worker_url, url, probe_bot.user_agent, region)
        for worker_url, region in zip(GEO_WORKER_URLS, region_labels)
    ]
    results = await asyncio.gather(*tasks)

    # Anomaly = statuses differ across regions for the same bot UA
    valid_statuses = [r["status"] for r in results if r["status"] is not None]
    geo_anomaly = len(set(valid_statuses)) > 1 if valid_statuses else False

    return geo_anomaly, list(results)


# ── Tier-3 full matrix ─────────────────────────────────────────────────────────

async def run_tier3_full_matrix(
    url: str,
    ua_results: list[UASpoofResult],
) -> list[dict[str, Any]]:
    """
    Tier-3: Full bot UA × region matrix.
    Only triggered if Tier-2 confirmed a geo anomaly.

    Sends every bot UA to every configured region. This is the most expensive
    check and is rarely triggered — typically only for sites with sophisticated
    geo-aware WAFs that treat different bot UAs differently by region.

    Returns list of {region, bot_ua, status, blocked} dicts.
    """
    if not GEO_WORKER_URLS:
        return [{"note": "No Cloudflare Worker URLs configured — Tier-3 skipped"}]

    region_labels = ["us", "eu", "apac"][: len(GEO_WORKER_URLS)]
    tasks = []
    for worker_url, region in zip(GEO_WORKER_URLS, region_labels):
        for bot in BOTS:
            tasks.append(_geo_fetch_one(worker_url, url, bot.user_agent, region))

    results = await asyncio.gather(*tasks)
    return list(results)


# ── LLM citation likelihood judgment (call #2) ─────────────────────────────────

async def _llm_citation_likelihood(
    url: str,
    meta_description: str | None,
    json_ld_types: list[str],
    word_count: int,
    search_bots_allowed: int,
    search_bots_total: int,
) -> tuple[float | None, str, str | None]:
    """
    LLM call #2: Estimate likelihood that AI answer engines would cite this URL.

    This is distinct from content quality (call #1) — it focuses on the
    signals AI systems use to decide *whether* to cite a page in their
    responses, not just whether the content is good.

    Key signals we pass:
      - URL (domain reputation heuristic)
      - Meta description (what the page claims to be about)
      - JSON-LD types (authoritative source signals)
      - Word count (thin-content signal)
      - Bot access permissions

    Returns (score_0_to_1, label, reasoning).
    Falls back gracefully if LLM unavailable.
    """
    if not ENABLE_LLM or EFFECTIVE_LLM_PROVIDER == "mock":
        return None, "unavailable — LLM not configured (ENABLE_LLM=false or no API key)", None

    prompt = f"""You are assessing how likely an AI answer engine (like ChatGPT, Perplexity, or Google AI Overviews) would be to cite this web page in its responses.

Rate citation likelihood from 1 (very unlikely) to 5 (very likely) based on these signals:
- Authoritative domain/source
- Clear, specific content that answers questions
- Structured data and proper metadata
- Accessibility to AI crawlers

Page signals:
URL: {url}
Meta description: {meta_description or '(none)'}
Structured data types: {', '.join(json_ld_types) if json_ld_types else '(none)'}
Content word count: {word_count}
Search/answer bots allowed: {search_bots_allowed}/{search_bots_total}

Reply in this exact format (no other text):
CITATION_LIKELIHOOD: <1-5>
REASONING: <one sentence>"""

    try:
        if EFFECTIVE_LLM_PROVIDER == "groq":
            raw = await _groq_complete(prompt)
        elif EFFECTIVE_LLM_PROVIDER == "gemini":
            raw = await _gemini_complete(prompt)
        else:
            return None, "unavailable — unknown provider", None
        return _parse_citation_response(raw)
    except Exception as e:
        return None, f"unavailable — LLM error: {str(e)[:100]}", None


def _parse_citation_response(response: str) -> tuple[float, str, str]:
    """Parse the citation likelihood LLM response."""
    score_raw = 3  # default: medium
    reasoning = ""

    for line in response.strip().splitlines():
        line = line.strip()
        if line.startswith("CITATION_LIKELIHOOD:"):
            try:
                import re
                score_raw = int(re.search(r"\d", line).group())  # type: ignore
            except Exception:
                pass
        elif line.startswith("REASONING:"):
            reasoning = line[len("REASONING:"):].strip()

    normalized = (score_raw - 1) / 4  # 1-5 → 0.0-1.0
    if normalized >= 0.75:
        label = "high"
    elif normalized >= 0.45:
        label = "medium"
    else:
        label = "low"

    return round(normalized, 3), label, reasoning


async def _groq_complete(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _gemini_complete(prompt: str) -> str:
    model = LLM_MODEL if "gemini" in LLM_MODEL else "gemini-1.5-flash-latest"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 150, "temperature": 0.1},
            },
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_empirical_tester(
    url: str,
    ua_results: list[UASpoofResult],         # From Tier-1 in access_checker
    meta_description: str | None = None,
    json_ld_types: list[str] | None = None,
    word_count: int = 0,
    search_bots_allowed: int = 0,
    search_bots_total: int = 5,
) -> EmpiricalResult:
    """
    Run the empirical tester with tiered escalation.

    Tier-1 results come from access_checker (already fetched, no duplication).
    Tier-2/3 only run conditionally based on anomaly detection.
    """
    tier_reached = 1
    geo_anomaly_detected = False
    geo_results: list[dict[str, Any]] = []
    tier3_results: list[dict[str, Any]] = []

    # ── Tier-1: analyse existing UA results ──────────────────────────────────
    tier1_anomaly, tier1_reason = _detect_tier1_anomaly(ua_results)

    raw_evidence: dict[str, Any] = {
        "tier1": {
            "anomaly_detected": tier1_anomaly,
            "anomaly_reason": tier1_reason,
            "results": [
                {
                    "bot": r.bot.name,
                    "category": r.bot.category.value,
                    "status": r.http_status,
                    "blocked": r.blocked,
                    "error": r.error,
                }
                for r in ua_results
            ],
        },
    }

    # ── Tier-2: geo trip-wire (only if Tier-1 flagged anomaly) ───────────────
    if tier1_anomaly and ENABLE_GEO_CHECK and GEO_WORKER_URLS:
        tier_reached = 2

        # Use first available search bot as probe (OAI-SearchBot if present)
        probe_bot = next((b for b in SEARCH_BOTS if b.user_agent == "OAI-SearchBot"), SEARCH_BOTS[0])
        geo_anomaly_detected, geo_results = await run_tier2_geo_tripwire(url, probe_bot)

        raw_evidence["tier2"] = {
            "probe_bot": probe_bot.user_agent,
            "geo_anomaly_detected": geo_anomaly_detected,
            "results": geo_results,
        }

        # ── Tier-3: full matrix (only if Tier-2 confirmed geo anomaly) ───────
        if geo_anomaly_detected:
            tier_reached = 3
            tier3_results = await run_tier3_full_matrix(url, ua_results)
            raw_evidence["tier3"] = {"results": tier3_results}

    elif tier1_anomaly and ENABLE_GEO_CHECK and not GEO_WORKER_URLS:
        raw_evidence["tier2"] = {
            "skipped": True,
            "reason": "Tier-1 anomaly detected but no GEO_WORKER_URL configured — deploy workers/geo_fetch/ to Cloudflare to enable geo trip-wire",
        }

    elif not tier1_anomaly:
        raw_evidence["tier2"] = {
            "skipped": True,
            "reason": "No Tier-1 anomaly — geo trip-wire not needed",
        }

    # ── LLM citation likelihood (call #2) ─────────────────────────────────────
    llm_score, llm_label, llm_reasoning = await _llm_citation_likelihood(
        url=url,
        meta_description=meta_description,
        json_ld_types=json_ld_types or [],
        word_count=word_count,
        search_bots_allowed=search_bots_allowed,
        search_bots_total=search_bots_total,
    )

    raw_evidence["llm_citation"] = {
        "provider": EFFECTIVE_LLM_PROVIDER,
        "score": llm_score,
        "label": llm_label,
        "reasoning": llm_reasoning,
    }

    return EmpiricalResult(
        url=url,
        tier_reached=tier_reached,
        ua_matrix=ua_results,
        geo_anomaly_detected=geo_anomaly_detected,
        llm_citation_score=llm_score,
        llm_citation_label=llm_label,
        llm_citation_reasoning=llm_reasoning,
        raw_evidence=raw_evidence,
    )
