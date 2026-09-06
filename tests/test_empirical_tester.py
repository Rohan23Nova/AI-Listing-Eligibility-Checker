"""
tests/test_empirical_tester.py — Unit tests for empirical_tester.

Tests cover:
  - Tier-1 anomaly detection: uniform pass, uniform fail, partial (anomaly)
  - Training vs search differential blocking (key spec requirement)
  - Tier-2 geo trip-wire skip when no anomaly detected
  - Tier-2 geo trip-wire skip when GEO_WORKER_URLS not configured
  - Tier-3 escalation triggered when Tier-2 detects geo anomaly
  - LLM citation likelihood stub behavior
  - run_empirical_tester integration
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from backend.checkers.empirical_tester import (
    _detect_tier1_anomaly,
    run_empirical_tester,
)
from backend.models import BOTS, SEARCH_BOTS, TRAINING_BOTS, UASpoofResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _ua_results(blocked_uas: set[str] | None = None, error_uas: set[str] | None = None) -> list[UASpoofResult]:
    blocked_uas = blocked_uas or set()
    error_uas = error_uas or set()
    results = []
    for bot in BOTS:
        if bot.user_agent in error_uas:
            results.append(UASpoofResult(bot=bot, http_status=None, redirect_chain=[], blocked=False, response_size_bytes=0, error="timeout"))
        elif bot.user_agent in blocked_uas:
            results.append(UASpoofResult(bot=bot, http_status=403, redirect_chain=[], blocked=True, response_size_bytes=0, error=None))
        else:
            results.append(UASpoofResult(bot=bot, http_status=200, redirect_chain=[], blocked=False, response_size_bytes=5000, error=None))
    return results


# ── _detect_tier1_anomaly tests ───────────────────────────────────────────────

def test_anomaly_none_when_all_ok():
    results = _ua_results()
    anomaly, reason = _detect_tier1_anomaly(results)
    assert anomaly is False
    assert "no" in reason.lower() or "uniform" in reason.lower()


def test_anomaly_none_when_all_blocked():
    blocked = {b.user_agent for b in BOTS}
    results = _ua_results(blocked_uas=blocked)
    anomaly, reason = _detect_tier1_anomaly(results)
    assert anomaly is False


def test_anomaly_detected_partial_blocking():
    """Some bots blocked, others not → anomaly."""
    blocked = {BOTS[0].user_agent, BOTS[1].user_agent}
    results = _ua_results(blocked_uas=blocked)
    anomaly, reason = _detect_tier1_anomaly(results)
    assert anomaly is True


def test_anomaly_detected_search_blocked_training_ok():
    """
    Key spec requirement: search bots blocked but training bots allowed.
    This should always flag an anomaly — it's the most impactful case.
    """
    search_blocked = {b.user_agent for b in SEARCH_BOTS}
    results = _ua_results(blocked_uas=search_blocked)
    anomaly, reason = _detect_tier1_anomaly(results)
    assert anomaly is True
    assert "search" in reason.lower()


def test_anomaly_detected_training_blocked_search_ok():
    """Training bots blocked but search bots fine → anomaly."""
    training_blocked = {b.user_agent for b in TRAINING_BOTS}
    results = _ua_results(blocked_uas=training_blocked)
    anomaly, reason = _detect_tier1_anomaly(results)
    assert anomaly is True
    assert "training" in reason.lower()


# ── run_empirical_tester integration tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_empirical_tester_no_anomaly_stays_tier1():
    """No Tier-1 anomaly → tier_reached=1, geo check skipped."""
    results = _ua_results()  # all 200

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False), \
         patch("backend.checkers.empirical_tester.ENABLE_GEO_CHECK", True):

        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.tier_reached == 1
    assert "tier2" in empirical.raw_evidence
    assert empirical.raw_evidence["tier2"].get("skipped") is True


@pytest.mark.asyncio
async def test_empirical_tester_anomaly_no_worker_urls():
    """
    Tier-1 anomaly detected but no GEO_WORKER_URLS → tier_reached=1,
    evidence explains why Tier-2 was skipped.
    """
    blocked = {b.user_agent for b in SEARCH_BOTS}
    results = _ua_results(blocked_uas=blocked)

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False), \
         patch("backend.checkers.empirical_tester.ENABLE_GEO_CHECK", True), \
         patch("backend.checkers.empirical_tester.GEO_WORKER_URLS", []):

        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.tier_reached == 1
    assert empirical.raw_evidence["tier2"]["skipped"] is True
    assert "GEO_WORKER_URL" in empirical.raw_evidence["tier2"]["reason"]


@pytest.mark.asyncio
async def test_empirical_tester_tier2_triggered_no_geo_anomaly():
    """
    Tier-1 anomaly + GEO_WORKER_URL configured → Tier-2 runs.
    Mock Worker returns same status from all regions → no geo anomaly.
    Tier-3 should NOT be triggered.
    """
    blocked = {b.user_agent for b in SEARCH_BOTS}
    results = _ua_results(blocked_uas=blocked)

    mock_geo_result = (False, [
        {"region": "us", "bot_ua": "OAI-SearchBot", "status": 403, "blocked": True, "error": None},
        {"region": "eu", "bot_ua": "OAI-SearchBot", "status": 403, "blocked": True, "error": None},
    ])

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False), \
         patch("backend.checkers.empirical_tester.ENABLE_GEO_CHECK", True), \
         patch("backend.checkers.empirical_tester.GEO_WORKER_URLS", ["https://worker-us.example.com", "https://worker-eu.example.com"]), \
         patch("backend.checkers.empirical_tester.run_tier2_geo_tripwire", new_callable=AsyncMock, return_value=mock_geo_result):

        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.tier_reached == 2
    assert empirical.geo_anomaly_detected is False
    assert "tier3" not in empirical.raw_evidence


@pytest.mark.asyncio
async def test_empirical_tester_tier3_triggered_on_geo_anomaly():
    """
    Geo anomaly detected → Tier-3 full matrix triggered.
    Verify tier_reached=3 and tier3 key in evidence.
    """
    blocked = {b.user_agent for b in SEARCH_BOTS}
    results = _ua_results(blocked_uas=blocked)

    geo_anomaly_result = (True, [
        {"region": "us", "bot_ua": "OAI-SearchBot", "status": 200, "blocked": False, "error": None},
        {"region": "eu", "bot_ua": "OAI-SearchBot", "status": 403, "blocked": True, "error": None},
    ])
    tier3_results = [{"region": "us", "bot_ua": "GPTBot", "status": 200, "blocked": False, "error": None}]

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False), \
         patch("backend.checkers.empirical_tester.ENABLE_GEO_CHECK", True), \
         patch("backend.checkers.empirical_tester.GEO_WORKER_URLS", ["https://worker-us.example.com", "https://worker-eu.example.com"]), \
         patch("backend.checkers.empirical_tester.run_tier2_geo_tripwire", new_callable=AsyncMock, return_value=geo_anomaly_result), \
         patch("backend.checkers.empirical_tester.run_tier3_full_matrix", new_callable=AsyncMock, return_value=tier3_results):

        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.tier_reached == 3
    assert empirical.geo_anomaly_detected is True
    assert "tier3" in empirical.raw_evidence


@pytest.mark.asyncio
async def test_empirical_tester_llm_stub_unavailable():
    """LLM citation returns 'unavailable' when ENABLE_LLM=False."""
    results = _ua_results()

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False):
        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.llm_citation_score is None
    assert "unavailable" in empirical.llm_citation_label.lower()


@pytest.mark.asyncio
async def test_empirical_tester_geo_check_disabled():
    """ENABLE_GEO_CHECK=False → Tier-2 never runs even with anomaly."""
    blocked = {b.user_agent for b in SEARCH_BOTS}
    results = _ua_results(blocked_uas=blocked)

    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False), \
         patch("backend.checkers.empirical_tester.ENABLE_GEO_CHECK", False):

        empirical = await run_empirical_tester(
            url="https://example.com",
            ua_results=results,
        )

    assert empirical.tier_reached == 1


@pytest.mark.asyncio
async def test_empirical_result_has_raw_evidence():
    """Every empirical result must include raw_evidence with tier1 key."""
    results = _ua_results()
    with patch("backend.checkers.empirical_tester.ENABLE_LLM", False):
        empirical = await run_empirical_tester("https://example.com", ua_results=results)

    assert "tier1" in empirical.raw_evidence
    assert "results" in empirical.raw_evidence["tier1"]
    assert len(empirical.raw_evidence["tier1"]["results"]) == len(BOTS)
