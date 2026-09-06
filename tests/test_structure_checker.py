"""
tests/test_structure_checker.py — Unit tests for structure_checker.

All tests operate on in-memory HTML strings — no network calls.
Covers:
  - JSON-LD: present/absent, valid, malformed, @graph format, multiple types
  - Meta: title, description, OG tags, noindex, noarchive, canonical
  - Heading hierarchy: H1 present, multiple H1, broken hierarchy, no headings
  - Canonical: present+self-ref, present+external, absent
"""

from __future__ import annotations

import pytest

from backend.checkers.structure_checker import (
    extract_json_ld,
    extract_meta_tags,
    analyze_heading_hierarchy,
    check_canonical,
    run_structure_checker,
)


# ── JSON-LD tests ─────────────────────────────────────────────────────────────

def test_json_ld_present_and_valid():
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Article","name":"Test"}
    </script></head><body></body></html>"""
    result = extract_json_ld(html)
    assert result.present is True
    assert result.malformed is False
    assert "Article" in result.types
    assert len(result.schemas) == 1


def test_json_ld_absent():
    html = "<html><head><title>No Schema</title></head><body></body></html>"
    result = extract_json_ld(html)
    assert result.present is False
    assert result.types == []
    assert result.schemas == []


def test_json_ld_malformed():
    html = """<html><head>
    <script type="application/ld+json">
    {this is not valid json
    </script></head><body></body></html>"""
    result = extract_json_ld(html)
    assert result.present is True
    assert result.malformed is True
    assert result.schemas == []


def test_json_ld_graph_format():
    html = """<html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "WebPage", "name": "Home"},
        {"@type": "Organization", "name": "ACME"}
      ]
    }
    </script></head></html>"""
    result = extract_json_ld(html)
    assert result.present is True
    assert "WebPage" in result.types
    assert "Organization" in result.types


def test_json_ld_multiple_types_in_list():
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":["NewsArticle","Article"]}
    </script></head></html>"""
    result = extract_json_ld(html)
    assert "NewsArticle" in result.types
    assert "Article" in result.types


def test_json_ld_multiple_scripts():
    html = """<html><head>
    <script type="application/ld+json">{"@type":"WebPage"}</script>
    <script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
    </head></html>"""
    result = extract_json_ld(html)
    assert "WebPage" in result.types
    assert "BreadcrumbList" in result.types
    assert len(result.schemas) == 2


# ── Meta tag tests ────────────────────────────────────────────────────────────

def test_meta_all_present():
    html = """<html><head>
    <title>My Page Title</title>
    <meta name="description" content="A great page about things.">
    <meta property="og:title" content="OG Title">
    <meta property="og:description" content="OG description here.">
    <meta property="og:image" content="https://example.com/img.jpg">
    <link rel="canonical" href="https://example.com/page">
    </head><body></body></html>"""
    result = extract_meta_tags(html, "https://example.com/page")
    assert result.title == "My Page Title"
    assert result.description == "A great page about things."
    assert result.og_title == "OG Title"
    assert result.og_description == "OG description here."
    assert result.og_image == "https://example.com/img.jpg"
    assert result.canonical == "https://example.com/page"
    assert result.noindex is False
    assert result.noarchive is False


def test_meta_noindex_detected():
    html = """<html><head>
    <meta name="robots" content="noindex, nofollow">
    </head></html>"""
    result = extract_meta_tags(html)
    assert result.noindex is True
    assert result.robots_meta == "noindex, nofollow"


def test_meta_noarchive_detected():
    html = """<html><head>
    <meta name="robots" content="noarchive">
    </head></html>"""
    result = extract_meta_tags(html)
    assert result.noarchive is True
    assert result.noindex is False


def test_meta_all_absent():
    html = "<html><head></head><body><p>Content</p></body></html>"
    result = extract_meta_tags(html)
    assert result.title is None
    assert result.description is None
    assert result.og_title is None
    assert result.noindex is False


def test_meta_case_insensitive_robots():
    """robots meta name should be case-insensitive."""
    html = '<html><head><meta name="Robots" content="NOINDEX"></head></html>'
    result = extract_meta_tags(html)
    assert result.noindex is True


# ── Heading hierarchy tests ───────────────────────────────────────────────────

def test_headings_perfect_hierarchy():
    html = """<html><body>
    <h1>Main Title</h1>
    <h2>Section 1</h2>
    <h3>Subsection 1.1</h3>
    <h2>Section 2</h2>
    </body></html>"""
    result = analyze_heading_hierarchy(html)
    assert result.has_h1 is True
    assert result.h1_count == 1
    assert result.hierarchy_issues == []


def test_headings_no_h1():
    html = """<html><body>
    <h2>Section A</h2>
    <h3>Subsection</h3>
    </body></html>"""
    result = analyze_heading_hierarchy(html)
    assert result.has_h1 is False
    assert any("H1" in issue for issue in result.hierarchy_issues)


def test_headings_multiple_h1():
    html = """<html><body>
    <h1>First Title</h1>
    <h1>Second Title</h1>
    </body></html>"""
    result = analyze_heading_hierarchy(html)
    assert result.h1_count == 2
    assert any("Multiple H1" in issue for issue in result.hierarchy_issues)


def test_headings_no_headings_at_all():
    html = "<html><body><p>Just a paragraph.</p></body></html>"
    result = analyze_heading_hierarchy(html)
    assert result.has_h1 is False
    assert any("No headings" in issue for issue in result.hierarchy_issues)


def test_headings_broken_hierarchy_h3_before_h2():
    html = """<html><body>
    <h1>Title</h1>
    <h3>Jumped to H3</h3>
    </body></html>"""
    result = analyze_heading_hierarchy(html)
    assert any("H3" in issue for issue in result.hierarchy_issues)


# ── Canonical tests ───────────────────────────────────────────────────────────

def test_canonical_self_referential():
    html = '<html><head><link rel="canonical" href="https://example.com/page"></head></html>'
    result = check_canonical(html, "https://example.com/page")
    assert result["present"] is True
    assert result["self_referential"] is True


def test_canonical_external():
    html = '<html><head><link rel="canonical" href="https://other.com/page"></head></html>'
    result = check_canonical(html, "https://example.com/page")
    assert result["present"] is True
    assert result["self_referential"] is False


def test_canonical_absent():
    html = "<html><head><title>No canonical</title></head></html>"
    result = check_canonical(html, "https://example.com/page")
    assert result["present"] is False


# ── run_structure_checker integration ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_structure_checker_with_html():
    """run_structure_checker accepts pre-fetched HTML — no network call needed."""
    html = """<html><head>
    <title>Test</title>
    <meta name="description" content="Test page description here.">
    <script type="application/ld+json">{"@type":"WebPage"}</script>
    </head><body>
    <h1>Welcome</h1>
    <h2>Section</h2>
    </body></html>"""

    result = await run_structure_checker("https://example.com", html=html)

    assert result.url == "https://example.com"
    assert result.error is None
    assert result.json_ld.present is True
    assert result.meta.title == "Test"
    assert result.meta.description == "Test page description here."
    assert result.headings.has_h1 is True
    assert result.headings.hierarchy_issues == []
    assert "json_ld" in result.raw_evidence
    assert "meta" in result.raw_evidence
    assert "headings" in result.raw_evidence


@pytest.mark.asyncio
async def test_run_structure_checker_noindex_detected():
    html = """<html><head>
    <meta name="robots" content="noindex">
    </head><body><h1>Title</h1></body></html>"""
    result = await run_structure_checker("https://example.com", html=html)
    assert result.meta.noindex is True
