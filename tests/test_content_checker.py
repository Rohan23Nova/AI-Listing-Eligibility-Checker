"""
tests/test_content_checker.py — Unit tests for content_checker.

Tests cover:
  - Visible text extraction (strips scripts, nav, footer)
  - Word counting
  - JS bundle heuristic — SPA detection (word count + bundle pattern)
  - Heuristic-only path (ENABLE_PLAYWRIGHT=False) — correctly flags SPA
    but skips render
  - Playwright escalation path (mocked) — uses rendered HTML when escalated
  - LLM quality stub behavior (unavailable when ENABLE_LLM=False)
  - Error handling: fetch failure → graceful ContentResult with error field
  - LLM response parsing edge cases
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from backend.checkers.content_checker import (
    _extract_main_content,
    _count_words,
    _detect_js_bundles,
    _is_likely_spa,
    _parse_llm_quality_response,
    run_content_checker,
    SPA_WORD_THRESHOLD,
)


# ── Text extraction tests ─────────────────────────────────────────────────────

def test_extract_main_content_strips_scripts():
    html = """<html><body>
    <script>var x = 1; document.write('invisible');</script>
    <style>.foo { color: red; }</style>
    <p>This is visible content.</p>
    </body></html>"""
    text, fallback = _extract_main_content(html)
    assert "invisible" not in text
    assert "color" not in text
    assert "visible content" in text


def test_extract_main_content_strips_nav_footer():
    html = """<html><body>
    <nav>Home About Contact</nav>
    <main><h1>Main Article</h1><p>Real content here.</p></main>
    <footer>Copyright 2025</footer>
    </body></html>"""
    text, fallback = _extract_main_content(html)
    assert "Real content here" in text
    # Nav/footer stripped
    assert "Copyright" not in text


def test_count_words_empty():
    assert _count_words("") == 0
    assert _count_words("   ") == 0


def test_count_words_normal():
    assert _count_words("hello world foo bar") == 4


# ── JS bundle heuristic ───────────────────────────────────────────────────────

def test_detect_js_bundles_react_app():
    html = """<html><head>
    <script src="/static/js/main.a1b2c3d4.chunk.js"></script>
    <script src="/static/js/vendor.abc123.js"></script>
    </head><body><div id="root"></div></body></html>"""
    has_bundles, bundle_urls = _detect_js_bundles(html)
    assert has_bundles is True
    assert len(bundle_urls) >= 1


def test_detect_js_bundles_simple_site():
    html = """<html><head>
    <script src="/jquery.min.js"></script>
    </head><body><p>Simple page.</p></body></html>"""
    has_bundles, bundle_urls = _detect_js_bundles(html)
    assert has_bundles is False


def test_detect_js_bundles_no_scripts():
    html = "<html><head></head><body><p>No JS at all.</p></body></html>"
    has_bundles, _ = _detect_js_bundles(html)
    assert has_bundles is False


def test_is_likely_spa_true():
    """Near-empty body + JS bundles = SPA."""
    assert _is_likely_spa(word_count=20, has_js_bundles=True) is True


def test_is_likely_spa_false_enough_words():
    """Enough visible words → not SPA, even with bundles."""
    assert _is_likely_spa(word_count=500, has_js_bundles=True) is False


def test_is_likely_spa_false_no_bundles():
    """Near-empty body but no bundles → not flagged as SPA."""
    assert _is_likely_spa(word_count=20, has_js_bundles=False) is False


def test_is_likely_spa_boundary():
    """Exactly at threshold is not flagged (threshold is strict <)."""
    assert _is_likely_spa(word_count=SPA_WORD_THRESHOLD, has_js_bundles=True) is False
    assert _is_likely_spa(word_count=SPA_WORD_THRESHOLD - 1, has_js_bundles=True) is True


# ── LLM response parsing ──────────────────────────────────────────────────────

def test_parse_llm_quality_response_perfect():
    response = """READABILITY: 5
SUBSTANTIVENESS: 5
CITABILITY: 5
REASONING: Excellent authoritative content."""
    score, label, reasoning = _parse_llm_quality_response(response)
    assert score == 1.0
    assert label == "high"
    assert "Excellent" in reasoning


def test_parse_llm_quality_response_poor():
    response = """READABILITY: 1
SUBSTANTIVENESS: 1
CITABILITY: 1
REASONING: Very thin content."""
    score, label, reasoning = _parse_llm_quality_response(response)
    assert score == 0.0
    assert label == "low"


def test_parse_llm_quality_response_medium():
    response = """READABILITY: 3
SUBSTANTIVENESS: 3
CITABILITY: 3
REASONING: Average quality."""
    score, label, reasoning = _parse_llm_quality_response(response)
    assert label == "medium"


def test_parse_llm_quality_response_malformed():
    """Malformed LLM response → default medium score, no crash."""
    score, label, reasoning = _parse_llm_quality_response("Sorry, I cannot help with that.")
    assert label == "medium"
    assert 0.0 <= score <= 1.0


# ── run_content_checker integration tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_content_checker_heuristic_only_path():
    """
    Test the heuristic-only path (ENABLE_PLAYWRIGHT=False, ENABLE_LLM=False).
    A content-rich page → likely_spa=False, playwright_used=False.
    """
    rich_html = "<html><body>" + "<p>Real content word. " * 200 + "</p></body></html>"

    with patch("backend.checkers.content_checker.fetch_html", return_value=(rich_html, 200, None)), \
         patch("backend.checkers.content_checker.ENABLE_PLAYWRIGHT", False), \
         patch("backend.checkers.content_checker.ENABLE_LLM", False), \
         patch("backend.llm_client.ENABLE_LLM", False):

        result = await run_content_checker("https://example.com")

    assert result.body_word_count > 100
    assert result.likely_spa is False
    assert result.playwright_used is False
    assert result.llm_quality_label == "unavailable" or "unavailable" in result.llm_quality_label
    assert result.error is None


@pytest.mark.asyncio
async def test_content_checker_spa_heuristic_fires_no_playwright():
    """
    SPA heuristic fires (near-empty body + JS bundles) but Playwright disabled.
    Result should flag likely_spa=True and set error explaining the situation.
    """
    spa_html = """<html><head>
    <script src="/static/js/main.abc123.chunk.js"></script>
    </head><body><div id="root"></div></body></html>"""

    with patch("backend.checkers.content_checker.fetch_html", return_value=(spa_html, 200, None)), \
         patch("backend.checkers.content_checker.ENABLE_PLAYWRIGHT", False), \
         patch("backend.checkers.content_checker.ENABLE_LLM", False):

        result = await run_content_checker("https://example.com")

    assert result.likely_spa is True
    assert result.playwright_used is False
    assert result.rendered_word_count is None
    # Error or note should mention Playwright is disabled
    assert result.error is not None
    assert "ENABLE_PLAYWRIGHT" in result.error


@pytest.mark.asyncio
async def test_content_checker_playwright_escalation_path():
    """
    Test the Playwright escalation path: SPA heuristic fires, Playwright enabled,
    Playwright returns rendered HTML with more words.
    """
    spa_html = """<html><head>
    <script src="/static/js/main.abc123.chunk.js"></script>
    </head><body><div id="root"></div></body></html>"""

    rendered_html = "<html><body>" + "<p>Rendered content word. " * 150 + "</p></body></html>"

    with patch("backend.checkers.content_checker.fetch_html", return_value=(spa_html, 200, None)), \
         patch("backend.checkers.content_checker.ENABLE_PLAYWRIGHT", True), \
         patch("backend.checkers.content_checker.ENABLE_LLM", False), \
         patch("backend.checkers.content_checker._render_with_playwright",
               new_callable=AsyncMock, return_value=(rendered_html, None)):

        result = await run_content_checker("https://example.com")

    assert result.likely_spa is True
    assert result.playwright_used is True
    assert result.rendered_word_count is not None
    assert result.rendered_word_count > result.body_word_count


@pytest.mark.asyncio
async def test_content_checker_playwright_escalation_error():
    """
    Playwright fails → result still returns (degraded), error is noted,
    analysis does NOT crash.
    """
    spa_html = """<html><head>
    <script src="/static/js/bundle.js"></script>
    </head><body></body></html>"""

    with patch("backend.checkers.content_checker.fetch_html", return_value=(spa_html, 200, None)), \
         patch("backend.checkers.content_checker.ENABLE_PLAYWRIGHT", True), \
         patch("backend.checkers.content_checker.ENABLE_LLM", False), \
         patch("backend.checkers.content_checker._render_with_playwright",
               new_callable=AsyncMock, return_value=(None, "Playwright timeout")):

        result = await run_content_checker("https://example.com")

    assert result.playwright_used is True
    assert result.rendered_word_count is None
    assert "Playwright" in (result.error or "")


@pytest.mark.asyncio
async def test_content_checker_fetch_error():
    """Page fetch failure → graceful ContentResult with error, no crash."""
    with patch("backend.checkers.content_checker.fetch_html",
               return_value=(None, None, "Connection refused")):

        result = await run_content_checker("https://unreachable.example.com")

    assert result.error is not None
    assert result.body_word_count == 0
    assert result.likely_spa is False

def test_extract_main_content_heuristic_strips_class_nav():
    html = """<html><body>
    <div class="site-navigation-menu">
       <ul><li>Home</li><li>Products</li><li>Contact</li><li>Blog</li><li>FAQ</li></ul>
       <p>Some extra nav text</p>
    </div>
    <div id="cookie-banner">Accept our cookies!</div>
    <div class="main-article-content">
       <p>This is the actual medical article about healthline stuff.</p>
       <p>It contains multiple paragraphs to be recognized as the main block.</p>
    </div>
    <div class="footer-widget">Footer links</div>
    </body></html>"""
    
    text, fallback = _extract_main_content(html)
    assert "actual medical article" in text
    assert "multiple paragraphs" in text
    assert "Home" not in text
    assert "Contact" not in text
    assert "cookie" not in text.lower()
    assert "Footer links" not in text
    assert fallback is False, "Should have successfully triggered main content heuristic"
