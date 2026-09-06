"""
tests/test_access_checker.py — Unit tests for access_checker.

All HTTP calls are mocked with respx so no real network calls are made.
Tests cover:
  - All bots allowed (happy path)
  - All bots denied
  - Training bots denied / search bots allowed (split case — key spec requirement)
  - Search bots denied / training bots allowed
  - UA-based HTTP blocking detection
  - sitemap.xml present vs absent
  - sitemap from robots.txt directive vs default path
  - llms.txt present vs absent
  - robots.txt fetch error (network failure → default-allow)
  - robots.txt 404 → default-allow
"""

from __future__ import annotations

import pytest
import respx
import httpx

from backend.checkers.access_checker import (
    parse_robots_txt,
    check_sitemap,
    check_llms_txt,
    tier1_ua_fetch_matrix,
    run_access_checker,
)
from backend.models import BotCategory, BOTS, SEARCH_BOTS, TRAINING_BOTS


# ── Fixtures / helpers ────────────────────────────────────────────────────────

EXAMPLE_URL = "https://example.com"
ROBOTS_URL = "https://example.com/robots.txt"
SITEMAP_URL = "https://example.com/sitemap.xml"
LLMS_URL = "https://example.com/llms.txt"


def _all_allow_robots() -> str:
    return "User-agent: *\nAllow: /\n"


def _all_deny_robots() -> str:
    lines = []
    for bot in BOTS:
        lines.append(f"User-agent: {bot.user_agent}")
        lines.append("Disallow: /")
        lines.append("")
    return "\n".join(lines)


def _training_deny_search_allow_robots() -> str:
    """Block all training bots; wildcard * allows search bots (not explicitly listed)."""
    lines = ["User-agent: *", "Allow: /", ""]
    for bot in TRAINING_BOTS:
        lines += [f"User-agent: {bot.user_agent}", "Disallow: /", ""]
    return "\n".join(lines)


def _search_deny_training_allow_robots() -> str:
    """Block search bots; wildcard * allows training bots."""
    lines = ["User-agent: *", "Allow: /", ""]
    for bot in SEARCH_BOTS:
        lines += [f"User-agent: {bot.user_agent}", "Disallow: /", ""]
    return "\n".join(lines)


SIMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc><lastmod>2025-01-01</lastmod></url>
  <url><loc>https://example.com/about</loc></url>
</urlset>"""


# ── robots.txt tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_robots_all_allow():
    """All bots should be allowed when robots.txt has wildcard Allow."""
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=_all_allow_robots()))
        async with httpx.AsyncClient() as client:
            per_bot, evidence, sitemap_urls = await parse_robots_txt(EXAMPLE_URL, client)

    assert len(per_bot) == len(BOTS)
    for ua, result in per_bot.items():
        assert result.allowed, f"{ua} should be allowed"
    assert evidence["http_status"] == 200
    assert sitemap_urls == []


@pytest.mark.asyncio
async def test_parse_robots_all_deny():
    """All bots should be denied when each has Disallow: /."""
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=_all_deny_robots()))
        async with httpx.AsyncClient() as client:
            per_bot, evidence, _ = await parse_robots_txt(EXAMPLE_URL, client)

    for ua, result in per_bot.items():
        assert not result.allowed, f"{ua} should be blocked"


@pytest.mark.asyncio
async def test_parse_robots_training_deny_search_allow():
    """
    Key spec requirement: training bots blocked, search/answer bots allowed.
    A site can legitimately block training crawlers without affecting AI search.
    """
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(
            return_value=httpx.Response(200, text=_training_deny_search_allow_robots())
        )
        async with httpx.AsyncClient() as client:
            per_bot, _, _ = await parse_robots_txt(EXAMPLE_URL, client)

    for bot in TRAINING_BOTS:
        assert not per_bot[bot.user_agent].allowed, f"Training bot {bot.user_agent} should be blocked"

    for bot in SEARCH_BOTS:
        assert per_bot[bot.user_agent].allowed, f"Search bot {bot.user_agent} should be allowed"


@pytest.mark.asyncio
async def test_parse_robots_search_deny_training_allow():
    """Opposite split: search bots blocked, training bots allowed."""
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(
            return_value=httpx.Response(200, text=_search_deny_training_allow_robots())
        )
        async with httpx.AsyncClient() as client:
            per_bot, _, _ = await parse_robots_txt(EXAMPLE_URL, client)

    for bot in SEARCH_BOTS:
        assert not per_bot[bot.user_agent].allowed, f"Search bot {bot.user_agent} should be blocked"

    for bot in TRAINING_BOTS:
        assert per_bot[bot.user_agent].allowed, f"Training bot {bot.user_agent} should be allowed"


@pytest.mark.asyncio
async def test_parse_robots_404_defaults_to_allow():
    """robots.txt 404 → RFC-compliant default: allow all bots."""
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404, text=""))
        async with httpx.AsyncClient() as client:
            per_bot, evidence, _ = await parse_robots_txt(EXAMPLE_URL, client)

    for ua, result in per_bot.items():
        assert result.allowed, f"{ua} should default to allowed"
    assert "default-allow" in evidence.get("parse_note", "")


@pytest.mark.asyncio
async def test_parse_robots_network_error_defaults_to_allow():
    """robots.txt network failure → default-allow (never crash)."""
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
        async with httpx.AsyncClient() as client:
            per_bot, evidence, _ = await parse_robots_txt(EXAMPLE_URL, client)

    for ua, result in per_bot.items():
        assert result.allowed
    assert evidence["fetch_error"] is not None


@pytest.mark.asyncio
async def test_parse_robots_with_sitemap_directive():
    """Sitemap: directive in robots.txt should be extracted."""
    robots_text = (
        "User-agent: *\nAllow: /\n\n"
        "Sitemap: https://example.com/sitemap.xml\n"
        "Sitemap: https://example.com/news-sitemap.xml\n"
    )
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots_text))
        async with httpx.AsyncClient() as client:
            _, evidence, sitemap_urls = await parse_robots_txt(EXAMPLE_URL, client)

    assert "https://example.com/sitemap.xml" in sitemap_urls
    assert "https://example.com/news-sitemap.xml" in sitemap_urls


@pytest.mark.asyncio
async def test_parse_robots_disallowed_paths():
    """Disallowed paths should be captured in result."""
    robots_text = (
        "User-agent: GPTBot\n"
        "Disallow: /private/\n"
        "Disallow: /admin/\n"
    )
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots_text))
        async with httpx.AsyncClient() as client:
            per_bot, _, _ = await parse_robots_txt(EXAMPLE_URL, client)

    gptbot = per_bot.get("GPTBot")
    assert gptbot is not None
    assert "/private/" in gptbot.disallowed_paths
    assert "/admin/" in gptbot.disallowed_paths


# ── sitemap tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_sitemap_found_at_default_path():
    async with respx.mock:
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=SIMPLE_SITEMAP))
        async with httpx.AsyncClient() as client:
            result = await check_sitemap(EXAMPLE_URL, client, [])

    assert result.exists is True
    assert result.url_count == 2
    assert result.last_modified == "2025-01-01"
    assert result.source == "default-path"


@pytest.mark.asyncio
async def test_check_sitemap_from_robots_directive():
    custom_sm = "https://example.com/custom-sitemap.xml"
    async with respx.mock:
        respx.get(custom_sm).mock(return_value=httpx.Response(200, text=SIMPLE_SITEMAP))
        async with httpx.AsyncClient() as client:
            result = await check_sitemap(EXAMPLE_URL, client, [custom_sm])

    assert result.exists is True
    assert result.source == "robots.txt"
    assert result.url == custom_sm


@pytest.mark.asyncio
async def test_check_sitemap_absent():
    async with respx.mock:
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(404, text="Not found"))
        async with httpx.AsyncClient() as client:
            result = await check_sitemap(EXAMPLE_URL, client, [])

    assert result.exists is False
    assert result.source == "absent"


# ── llms.txt tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_llms_txt_present():
    async with respx.mock:
        respx.get(LLMS_URL).mock(return_value=httpx.Response(200, text="name: Example Site\n"))
        async with httpx.AsyncClient() as client:
            result = await check_llms_txt(EXAMPLE_URL, client)

    assert result.exists is True
    assert result.has_content is True
    # The note must mention "bonus" to satisfy the spec
    assert "bonus" in result.note.lower()


@pytest.mark.asyncio
async def test_check_llms_txt_absent():
    async with respx.mock:
        respx.get(LLMS_URL).mock(return_value=httpx.Response(404, text=""))
        async with httpx.AsyncClient() as client:
            result = await check_llms_txt(EXAMPLE_URL, client)

    assert result.exists is False


# ── UA-spoof / integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ua_blocking_detected():
    """
    A site that returns 200 to * but 403 to specific bot UAs should be flagged.
    This is a Tier-1 empirical check — it detects UA-based blocking even when
    robots.txt says Allow: /.
    """
    blocked_uas = {"GPTBot", "ClaudeBot"}

    def side_effect(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        if ua in blocked_uas:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="<html><body>Hello</body></html>")

    async with respx.mock:
        respx.get(EXAMPLE_URL).mock(side_effect=side_effect)
        results = await tier1_ua_fetch_matrix(EXAMPLE_URL)

    blocked = [r for r in results if r.blocked]
    not_blocked = [r for r in results if not r.blocked and r.error is None]

    blocked_names = {r.bot.user_agent for r in blocked}
    assert "GPTBot" in blocked_names
    assert "ClaudeBot" in blocked_names
    # Non-blocked bots should not be in blocked set
    assert "PerplexityBot" not in blocked_names


@pytest.mark.asyncio
async def test_ua_network_error_not_counted_as_blocked():
    """Network errors (timeouts, DNS failures) should not be flagged as blocked=True."""
    async with respx.mock:
        respx.get(EXAMPLE_URL).mock(side_effect=httpx.ConnectError("DNS failure"))
        results = await tier1_ua_fetch_matrix(EXAMPLE_URL)

    for r in results:
        assert r.error is not None
        assert r.blocked is False  # error ≠ blocked


@pytest.mark.asyncio
async def test_run_access_checker_integration():
    """
    Integration test for run_access_checker: mocks all 3 endpoints and checks
    that the returned AccessResult is complete and well-formed.
    """
    async with respx.mock:
        respx.get(ROBOTS_URL).mock(
            return_value=httpx.Response(200, text=_training_deny_search_allow_robots())
        )
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=SIMPLE_SITEMAP))
        respx.get(LLMS_URL).mock(return_value=httpx.Response(404, text=""))
        # All UA-spoof fetches return 200
        respx.get(EXAMPLE_URL).mock(return_value=httpx.Response(200, text="<html><body>OK</body></html>"))

        result = await run_access_checker(EXAMPLE_URL)

    assert result.url == EXAMPLE_URL
    assert len(result.robots_per_bot) == len(BOTS)
    assert result.sitemap.exists is True
    assert result.llms_txt.exists is False

    # Training bots should be blocked, search bots allowed
    robots_by_ua = {r.bot.user_agent: r for r in result.robots_per_bot}
    for bot in TRAINING_BOTS:
        assert not robots_by_ua[bot.user_agent].allowed
    for bot in SEARCH_BOTS:
        assert robots_by_ua[bot.user_agent].allowed

    # Raw evidence must be present
    assert "robots" in result.raw_evidence
    assert "sitemap" in result.raw_evidence
    assert "ua_spoof" in result.raw_evidence
