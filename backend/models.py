"""
models.py — Shared data models (dataclasses / TypedDicts) used across all modules.

Using dataclasses for internal objects and Pydantic BaseModel for FastAPI
request/response schemas.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Bot taxonomy ──────────────────────────────────────────────────────────────

class BotCategory(str, Enum):
    """
    Distinguishes the purpose of an AI crawler.

    TRAINING  — bots that collect data for model training (often can be
                blocked without affecting search visibility).
    SEARCH    — bots that power search/answer surfaces (blocking these
                removes the site from AI search results).
    """
    TRAINING = "training"
    SEARCH = "search"


@dataclass(frozen=True)
class BotSpec:
    """Canonical definition of one AI crawler."""
    name: str               # Human-readable name, e.g. "GPTBot"
    user_agent: str         # Exact UA token used in robots.txt and HTTP requests
    category: BotCategory
    provider: str           # "OpenAI", "Anthropic", etc.
    description: str        # One-line description shown in the UI


# ── Canonical bot list ────────────────────────────────────────────────────────
# Kept in one place so every module uses the same source of truth.
# Categories:
#   TRAINING — collects data for model training; blocking ≠ losing search placement
#   SEARCH   — powers live answer/search surfaces; blocking = disappearing from AI search

BOTS: list[BotSpec] = [
    # ── Training bots ──────────────────────────────────────────────────────
    BotSpec(
        name="GPTBot",
        user_agent="GPTBot",
        category=BotCategory.TRAINING,
        provider="OpenAI",
        description="OpenAI training data crawler",
    ),
    BotSpec(
        name="ClaudeBot",
        user_agent="ClaudeBot",
        category=BotCategory.TRAINING,
        provider="Anthropic",
        description="Anthropic training data crawler",
    ),
    BotSpec(
        name="Google-Extended",
        user_agent="Google-Extended",
        category=BotCategory.TRAINING,
        provider="Google",
        description="Google Bard/Gemini training data crawler",
    ),
    BotSpec(
        name="CCBot",
        user_agent="CCBot",
        category=BotCategory.TRAINING,
        provider="Common Crawl",
        description="Common Crawl — used by many LLM training pipelines",
    ),
    BotSpec(
        name="Amazonbot",
        user_agent="Amazonbot",
        category=BotCategory.TRAINING,
        provider="Amazon",
        description="Amazon Alexa / AI training crawler",
    ),
    BotSpec(
        name="Diffbot",
        user_agent="Diffbot",
        category=BotCategory.TRAINING,
        provider="Diffbot",
        description="Diffbot AI knowledge graph crawler",
    ),
    # ── Search / Answer bots ───────────────────────────────────────────────
    BotSpec(
        name="OAI-SearchBot",
        user_agent="OAI-SearchBot",
        category=BotCategory.SEARCH,
        provider="OpenAI",
        description="ChatGPT Search / Browse-with-Bing crawler",
    ),
    BotSpec(
        name="Claude-SearchBot",
        user_agent="Claude-SearchBot",
        category=BotCategory.SEARCH,
        provider="Anthropic",
        description="Claude web search crawler",
    ),
    BotSpec(
        name="PerplexityBot",
        user_agent="PerplexityBot",
        category=BotCategory.SEARCH,
        provider="Perplexity",
        description="Perplexity AI search and answer crawler",
    ),
    BotSpec(
        name="Applebot-Extended",
        user_agent="Applebot-Extended",
        category=BotCategory.SEARCH,
        provider="Apple",
        description="Apple AI search / Siri Knowledge crawler",
    ),
    BotSpec(
        name="Googlebot",
        user_agent="Googlebot",
        category=BotCategory.SEARCH,
        provider="Google",
        description="Google Search crawler (also feeds AI Overviews)",
    ),
]

# Lookup helpers
BOT_BY_UA: dict[str, BotSpec] = {b.user_agent: b for b in BOTS}
TRAINING_BOTS = [b for b in BOTS if b.category == BotCategory.TRAINING]
SEARCH_BOTS = [b for b in BOTS if b.category == BotCategory.SEARCH]


# ── Access checker results ────────────────────────────────────────────────────

@dataclass
class BotAccessResult:
    """robots.txt access result for a single bot."""
    bot: BotSpec
    allowed: bool                   # Can the bot access the root path?
    disallowed_paths: list[str]      # Paths explicitly disallowed
    crawl_delay: float | None       # robots.txt Crawl-delay directive, if present
    rule_source: str                # "robots.txt" | "default-allow" | "default-deny"


@dataclass
class UASpoofResult:
    """Result of a UA-spoofed HTTP fetch for one bot."""
    bot: BotSpec
    http_status: int | None         # None if request failed
    redirect_chain: list[str]       # URLs followed before final response
    blocked: bool                   # True if status is 403/429/robots-blocked
    response_size_bytes: int        # Size of response body
    error: str | None               # Network/timeout error message, if any


@dataclass
class SitemapResult:
    exists: bool
    url: str | None
    url_count: int | None           # Number of <url> entries found
    last_modified: str | None       # ISO date string from lastmod, if present
    source: str                     # "robots.txt" | "default-path" | "absent"


@dataclass
class LlmsTxtResult:
    """
    /llms.txt check result.

    IMPORTANT: llms.txt has no standards-body backing and no confirmed adoption
    by any AI provider. It is tracked here as a bonus signal only and must never
    be used as a pass/fail gate. The UI must make this clear.
    """
    exists: bool
    url: str | None
    has_content: bool               # Non-empty file
    note: str = (
        "llms.txt has no confirmed adoption by AI providers and carries no "
        "guarantee of indexing. Treat as a minor bonus signal only."
    )


@dataclass
class AccessResult:
    """Aggregate result from access_checker."""
    url: str
    robots_per_bot: list[BotAccessResult]
    sitemap: SitemapResult
    llms_txt: LlmsTxtResult
    ua_spoof_results: list[UASpoofResult]
    raw_evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ── Structure checker results ─────────────────────────────────────────────────

@dataclass
class JsonLdResult:
    present: bool
    schemas: list[dict[str, Any]]   # Parsed JSON-LD objects
    types: list[str]                # @type values found (e.g. "Article", "Organization")
    malformed: bool                 # True if JSON-LD exists but fails to parse
    error: str | None = None


@dataclass
class MetaTagResult:
    title: str | None
    description: str | None
    og_title: str | None
    og_description: str | None
    og_image: str | None
    canonical: str | None
    robots_meta: str | None         # Content of <meta name="robots">
    noindex: bool                   # True if "noindex" appears in robots meta
    noarchive: bool                 # True if "noarchive" appears in robots meta


@dataclass
class HeadingResult:
    has_h1: bool
    h1_count: int
    heading_text: list[tuple[str, str]]   # [(tag, text), ...] e.g. [("h1", "Welcome")]
    hierarchy_issues: list[str]           # e.g. ["H3 before H2", "Multiple H1s"]


@dataclass
class StructureResult:
    """Aggregate result from structure_checker."""
    url: str
    json_ld: JsonLdResult
    meta: MetaTagResult
    headings: HeadingResult
    raw_evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ── Content checker results ───────────────────────────────────────────────────

@dataclass
class ContentResult:
    """Result from content_checker (M5 — filled with stub in Checkpoint 1)."""
    url: str
    body_word_count: int
    likely_spa: bool                # Heuristic: near-empty body + large JS bundle
    playwright_used: bool           # Was Playwright escalated?
    rendered_word_count: int | None # Word count after JS render (if Playwright used)
    llm_quality_score: float | None # 0.0–1.0, None if LLM unavailable
    llm_quality_label: str          # e.g. "high", "medium", "low", "unavailable"
    llm_quality_reasoning: str | None
    raw_evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ── Empirical tester results ──────────────────────────────────────────────────

@dataclass
class EmpiricalResult:
    """Result from empirical_tester (M6 — stub in Checkpoint 1)."""
    url: str
    tier_reached: int               # 1, 2, or 3
    ua_matrix: list[UASpoofResult]  # Tier-1 results
    geo_anomaly_detected: bool      # Tier-2 trip-wire fired
    llm_citation_score: float | None
    llm_citation_label: str
    llm_citation_reasoning: str | None
    raw_evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ── Score result ──────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdownItem:
    """One scoring sub-component — every point is explainable."""
    label: str          # Human-readable name
    points_earned: float
    points_possible: float
    reason: str         # Why this score was given
    evidence: Any       # The raw data that determined the score


@dataclass
class ScoreResult:
    total: float                            # 0–100
    max_possible: float                     # Usually 100, may be lower if LLM unavailable
    verdict: str                            # "Excellent" | "Good" | "Fair" | "Poor"
    breakdown: list[ScoreBreakdownItem]
    issues: list[dict[str, str]]            # [{severity, title, fix}, ...]
    llm_available: bool


# ── Final report ──────────────────────────────────────────────────────────────

@dataclass
class PerBotRow:
    """One row in the per-bot access table shown in the UI."""
    bot_name: str
    provider: str
    category: str       # "training" | "search"
    robots_allowed: bool
    ua_http_status: int | None
    ua_blocked: bool
    crawl_delay: float | None
    disallowed_paths: list[str]


@dataclass
class FixPackItem:
    filename: str
    description: str
    content: str        # The actual text content to download


@dataclass
class Report:
    """Top-level report object returned by /analyze."""
    url: str
    timestamp: str              # ISO 8601
    score: ScoreResult
    per_bot_table: list[PerBotRow]
    training_bots_summary: str  # e.g. "3/6 training bots allowed"
    search_bots_summary: str    # e.g. "5/5 search bots allowed"
    raw_evidence: dict[str, Any]
    fix_pack: list[FixPackItem]
    analysis_duration_ms: int
    llms_txt_note: str = (
        "llms.txt has no confirmed adoption by AI providers and carries no "
        "guarantee of indexing. It is tracked as a minor bonus signal only."
    )
