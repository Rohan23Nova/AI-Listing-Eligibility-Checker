"""
checkers/structure_checker.py — HTML structure and semantic markup analysis.

Checks:
  1. JSON-LD schema markup (presence, @type, malformed detection)
  2. Meta tags (title, description, OG, robots meta — noindex/noarchive)
  3. Heading hierarchy (H1 presence, logical H1→H2→H3 ordering)
  4. Canonical URL tag

These checks are entirely deterministic — no network I/O beyond the initial
page fetch, no LLM calls. The HTML is fetched once and all checks run on it.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from backend.config import HTTP_TIMEOUT_CHEAP, DEFAULT_HEADERS
from backend.models import (
    HeadingResult,
    JsonLdResult,
    MetaTagResult,
    StructureResult,
)

import httpx


# ── Page fetch ────────────────────────────────────────────────────────────────

async def fetch_page_html(url: str) -> tuple[str | None, int | None, str | None]:
    """
    Fetch the raw HTML of the page using a neutral browser-like UA.
    Returns (html, status_code, error).
    """
    headers = {
        **DEFAULT_HEADERS,
        "User-Agent": (
            "Mozilla/5.0 (compatible; AI-Eligibility-Checker/1.0; "
            "+https://github.com/Rohan23Nova/AI-Listing-Eligibility-Checker)"
        ),
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=HTTP_TIMEOUT_CHEAP,
        ) as client:
            resp = await client.get(url)
            return resp.text, resp.status_code, None
    except httpx.TimeoutException:
        return None, None, f"timeout after {HTTP_TIMEOUT_CHEAP}s"
    except httpx.RequestError as e:
        return None, None, str(e)


# ── JSON-LD extraction ────────────────────────────────────────────────────────

def extract_json_ld(html: str) -> JsonLdResult:
    """
    Find all <script type="application/ld+json"> blocks, parse them, and
    extract @type values. Returns JsonLdResult with presence flag, parsed
    objects, type list, and malformed flag.
    """
    soup = BeautifulSoup(html, "lxml")
    scripts = soup.find_all("script", type="application/ld+json")

    if not scripts:
        return JsonLdResult(present=False, schemas=[], types=[], malformed=False)

    schemas: list[dict[str, Any]] = []
    types: list[str] = []
    malformed = False

    for script in scripts:
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            malformed = True
            continue

        # Handle both single object and @graph array
        if isinstance(data, list):
            for item in data:
                schemas.append(item)
                if isinstance(item, dict) and "@type" in item:
                    t = item["@type"]
                    if isinstance(t, list):
                        types.extend(t)
                    else:
                        types.append(str(t))
        elif isinstance(data, dict):
            schemas.append(data)
            # @graph
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    if isinstance(item, dict) and "@type" in item:
                        t = item["@type"]
                        if isinstance(t, list):
                            types.extend(t)
                        else:
                            types.append(str(t))
            elif "@type" in data:
                t = data["@type"]
                if isinstance(t, list):
                    types.extend(t)
                else:
                    types.append(str(t))

    return JsonLdResult(
        present=True,
        schemas=schemas,
        types=list(set(types)),  # deduplicate
        malformed=malformed,
    )


# ── Meta tag extraction ───────────────────────────────────────────────────────

def extract_meta_tags(html: str, page_url: str = "") -> MetaTagResult:
    """
    Extract title, description, OG tags, canonical, and robots meta.
    The robots meta tag is particularly important: noindex means AI crawlers
    that respect meta-robots (most do) will not index the page.
    """
    soup = BeautifulSoup(html, "lxml")

    # <title>
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    def _meta(name: str | None = None, prop: str | None = None) -> str | None:
        """Find a meta tag by name or property attribute."""
        if name:
            tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
        elif prop:
            tag = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)})
        else:
            return None
        return tag.get("content", "").strip() if tag else None  # type: ignore[union-attr]

    description = _meta(name="description")
    og_title = _meta(prop="og:title")
    og_description = _meta(prop="og:description")
    og_image = _meta(prop="og:image")
    robots_meta = _meta(name="robots")

    # Canonical
    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else None  # type: ignore[union-attr]

    # noindex / noarchive from robots meta
    robots_lower = (robots_meta or "").lower()
    noindex = "noindex" in robots_lower
    noarchive = "noarchive" in robots_lower

    return MetaTagResult(
        title=title,
        description=description,
        og_title=og_title,
        og_description=og_description,
        og_image=og_image,
        canonical=canonical,
        robots_meta=robots_meta,
        noindex=noindex,
        noarchive=noarchive,
    )


# ── Heading hierarchy analysis ────────────────────────────────────────────────

def analyze_heading_hierarchy(html: str) -> HeadingResult:
    """
    Analyze heading tags for AI-readability signals:
      - Presence of exactly one H1 (multiple H1s can confuse crawlers)
      - Logical nesting (no H3 before H2, no H2 before H1, etc.)
      - Absence of any headings (pure body copy is harder for AI to chunk)
    """
    soup = BeautifulSoup(html, "lxml")
    heading_tags = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])

    heading_text: list[tuple[str, str]] = []
    for tag in heading_tags:
        heading_text.append((tag.name, tag.get_text(strip=True)[:120]))

    h1_count = sum(1 for tag, _ in heading_text if tag == "h1")
    has_h1 = h1_count > 0

    # Check hierarchy: no heading level should appear before its parent
    issues: list[str] = []
    if not heading_text:
        issues.append("No headings found — content structure is invisible to AI chunkers")
    elif not has_h1:
        issues.append("No H1 tag — AI crawlers rely on H1 as the primary topic signal")
    elif h1_count > 1:
        issues.append(f"Multiple H1 tags ({h1_count}) — should have exactly one H1 per page")

    seen_levels: set[int] = set()
    for tag, _ in heading_text:
        level = int(tag[1])
        if level > 1:
            # Allow any level if a heading exists that's at most one level above
            # Flag if a deeper level appears before its immediate parent was ever seen
            if (level - 1) not in seen_levels and level not in seen_levels:
                issues.append(f"H{level} appears before H{level - 1} — broken heading hierarchy")
        seen_levels.add(level)

    return HeadingResult(
        has_h1=has_h1,
        h1_count=h1_count,
        heading_text=heading_text[:20],  # cap for storage
        hierarchy_issues=issues,
    )


# ── Canonical check helper ────────────────────────────────────────────────────

def check_canonical(html: str, page_url: str) -> dict[str, Any]:
    """
    Check canonical tag: is it present, does it point to self, or does
    it redirect AI crawlers to a different authoritative page?
    """
    soup = BeautifulSoup(html, "lxml")
    canonical_tag = soup.find("link", rel="canonical")

    if not canonical_tag:
        return {"present": False, "self_referential": None, "canonical_url": None}

    canonical_href = canonical_tag.get("href", "").strip()  # type: ignore[union-attr]

    # Normalize both URLs for comparison (remove trailing slash)
    def _normalize(u: str) -> str:
        return u.rstrip("/").lower().split("?")[0]

    self_ref = _normalize(canonical_href) == _normalize(page_url)

    return {
        "present": True,
        "self_referential": self_ref,
        "canonical_url": canonical_href,
        "page_url": page_url,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_structure_checker(url: str, html: str | None = None) -> StructureResult:
    """
    Run all structure checks for the given URL.
    If html is provided (e.g. from content_checker's Playwright render),
    it is used directly. Otherwise the page is fetched fresh.
    """
    error: str | None = None

    if html is None:
        html, status, fetch_error = await fetch_page_html(url)
        if fetch_error or not html:
            return StructureResult(
                url=url,
                json_ld=JsonLdResult(present=False, schemas=[], types=[], malformed=False),
                meta=MetaTagResult(
                    title=None, description=None, og_title=None, og_description=None,
                    og_image=None, canonical=None, robots_meta=None,
                    noindex=False, noarchive=False,
                ),
                headings=HeadingResult(has_h1=False, h1_count=0, heading_text=[], hierarchy_issues=[]),
                raw_evidence={"fetch_error": fetch_error, "http_status": status},
                error=fetch_error or "Empty HTML response",
            )

    json_ld = extract_json_ld(html)
    meta = extract_meta_tags(html, url)
    headings = analyze_heading_hierarchy(html)
    canonical_info = check_canonical(html, url)

    raw_evidence: dict[str, Any] = {
        "json_ld": {
            "present": json_ld.present,
            "types": json_ld.types,
            "schema_count": len(json_ld.schemas),
            "malformed": json_ld.malformed,
        },
        "meta": {
            "title": meta.title,
            "description": meta.description[:200] if meta.description else None,
            "og_title": meta.og_title,
            "og_description": meta.og_description[:200] if meta.og_description else None,
            "og_image": meta.og_image,
            "canonical": meta.canonical,
            "robots_meta": meta.robots_meta,
            "noindex": meta.noindex,
            "noarchive": meta.noarchive,
        },
        "headings": {
            "has_h1": headings.has_h1,
            "h1_count": headings.h1_count,
            "total_headings": len(headings.heading_text),
            "hierarchy_issues": headings.hierarchy_issues,
            "sample": headings.heading_text[:5],
        },
        "canonical": canonical_info,
    }

    return StructureResult(
        url=url,
        json_ld=json_ld,
        meta=meta,
        headings=headings,
        raw_evidence=raw_evidence,
        error=error,
    )
