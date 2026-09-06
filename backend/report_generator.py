"""
report_generator.py — Assembles the final structured report from all checker results.

Produces:
  - score + verdict
  - per-bot table (training vs search explicitly separated)
  - prioritized issues with fix descriptions
  - raw evidence section (every score has its backing data)
  - fix-pack preview (robots.txt additions, llms.txt template, JSON-LD snippet)
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

from backend.models import (
    AccessResult,
    BotCategory,
    ContentResult,
    EmpiricalResult,
    FixPackItem,
    PerBotRow,
    Report,
    ScoreResult,
    StructureResult,
    BOTS,
    TRAINING_BOTS,
    SEARCH_BOTS,
)


def _build_per_bot_table(access: AccessResult) -> list[PerBotRow]:
    """Build the per-bot table for the UI, merging robots.txt + UA data."""
    robots_by_ua = {r.bot.user_agent: r for r in access.robots_per_bot}
    ua_by_ua = {r.bot.user_agent: r for r in access.ua_spoof_results}

    rows: list[PerBotRow] = []
    for bot in BOTS:
        robots_r = robots_by_ua.get(bot.user_agent)
        ua_r = ua_by_ua.get(bot.user_agent)
        rows.append(PerBotRow(
            bot_name=bot.name,
            provider=bot.provider,
            category=bot.category.value,
            robots_allowed=robots_r.allowed if robots_r else True,
            ua_http_status=ua_r.http_status if ua_r else None,
            ua_blocked=ua_r.blocked if ua_r else False,
            crawl_delay=robots_r.crawl_delay if robots_r else None,
            disallowed_paths=robots_r.disallowed_paths if robots_r else [],
        ))
    return rows


def _build_fix_pack(
    access: AccessResult,
    structure: StructureResult,
    score: ScoreResult,
) -> list[FixPackItem]:
    """
    Generate downloadable fix-pack items based on detected issues.
    Only generates items where fixes are actually needed.
    """
    items: list[FixPackItem] = []

    # ── robots.txt additions ──────────────────────────────────────────────────
    robots_by_ua = {r.bot.user_agent: r for r in access.robots_per_bot}

    blocked_search = [
        bot for bot in SEARCH_BOTS
        if not (robots_by_ua.get(bot.user_agent) and robots_by_ua[bot.user_agent].allowed)
    ]
    blocked_training = [
        bot for bot in TRAINING_BOTS
        if not (robots_by_ua.get(bot.user_agent) and robots_by_ua[bot.user_agent].allowed)
    ]

    if blocked_search or blocked_training:
        lines = [
            "# === AI Crawler Access — add to your existing robots.txt ===",
            "",
            "# Search/Answer bots (blocking these removes you from AI search results)",
        ]
        for bot in SEARCH_BOTS:
            if bot.user_agent not in [b.user_agent for b in blocked_search]:
                continue  # already allowed
            lines += [f"User-agent: {bot.user_agent}", "Allow: /", ""]

        lines += [
            "# Training bots (blocking these is your choice — it won't affect AI search)",
            "# Uncomment the blocks below only if you WANT to allow training crawlers.",
        ]
        for bot in TRAINING_BOTS:
            if bot.user_agent not in [b.user_agent for b in blocked_training]:
                continue
            lines += [f"# User-agent: {bot.user_agent}", "# Allow: /", ""]

        items.append(FixPackItem(
            filename="robots_txt_additions.txt",
            description="Paste these User-agent blocks into your robots.txt to fix blocked bots",
            content="\n".join(lines),
        ))

    # ── llms.txt template ──────────────────────────────────────────────────────
    if not access.llms_txt.exists:
        items.append(FixPackItem(
            filename="llms.txt",
            description=(
                "Optional /llms.txt template. "
                "IMPORTANT: This file has no confirmed adoption by AI providers. "
                "It may have future value but is tracked here as a bonus signal only."
            ),
            content=(
                "# llms.txt — AI-readable site summary\n"
                "# Place this file at your domain root: https://yourdomain.com/llms.txt\n"
                "# NOTE: As of 2025, no major AI provider has confirmed they use this file.\n"
                "# Include it as a forward-compatible signal only.\n\n"
                "# Site name\n"
                "name: Your Site Name\n\n"
                "# Brief description for AI systems\n"
                "description: >\n"
                "  A brief description of your site's content and purpose.\n\n"
                "# Contact for AI-related queries\n"
                "contact: ai@yourdomain.com\n"
            ),
        ))

    # ── JSON-LD snippet ────────────────────────────────────────────────────────
    if not structure.json_ld.present or structure.json_ld.malformed:
        items.append(FixPackItem(
            filename="json_ld_snippet.html",
            description="Add this <script> block to your page <head> for structured data",
            content=(
                '<script type="application/ld+json">\n'
                "{\n"
                '  "@context": "https://schema.org",\n'
                '  "@type": "WebPage",\n'
                '  "name": "Your Page Title",\n'
                '  "description": "A clear description of this page\'s content.",\n'
                '  "url": "https://yourdomain.com/this-page",\n'
                '  "author": {\n'
                '    "@type": "Organization",\n'
                '    "name": "Your Organization"\n'
                "  },\n"
                '  "dateModified": "2025-01-01"\n'
                "}\n"
                "</script>"
            ),
        ))

    # ── Meta description template ──────────────────────────────────────────────
    if not structure.meta.description:
        items.append(FixPackItem(
            filename="meta_description.html",
            description="Add this tag to your page <head>",
            content='<meta name="description" content="A 120-160 character description of this page\'s content.">',
        ))

    return items


def generate_report(
    url: str,
    access: AccessResult,
    structure: StructureResult,
    score: ScoreResult,
    content: ContentResult | None = None,
    empirical: EmpiricalResult | None = None,
    duration_ms: int = 0,
) -> Report:
    """
    Assemble the final Report from all checker results and the computed score.
    """
    per_bot_table = _build_per_bot_table(access)

    # Summaries for training vs search split
    training_allowed = sum(1 for r in per_bot_table if r.category == "training" and r.robots_allowed)
    training_total = len(TRAINING_BOTS)
    search_allowed = sum(1 for r in per_bot_table if r.category == "search" and r.robots_allowed)
    search_total = len(SEARCH_BOTS)

    raw_evidence = {
        "access": access.raw_evidence,
        "structure": structure.raw_evidence,
        "content": content.raw_evidence if content else None,
        "empirical": empirical.raw_evidence if empirical else None,
        "score_breakdown": [
            {
                "label": item.label,
                "earned": item.points_earned,
                "possible": item.points_possible,
                "reason": item.reason,
            }
            for item in score.breakdown
        ],
    }

    fix_pack = _build_fix_pack(access, structure, score)

    return Report(
        url=url,
        timestamp=datetime.now(timezone.utc).isoformat(),
        score=score,
        per_bot_table=per_bot_table,
        training_bots_summary=f"{training_allowed}/{training_total} training bots allowed",
        search_bots_summary=f"{search_allowed}/{search_total} search/answer bots allowed",
        raw_evidence=raw_evidence,
        fix_pack=fix_pack,
        analysis_duration_ms=duration_ms,
    )


def report_to_dict(report: Report) -> dict[str, Any]:
    """
    Serialize a Report to a plain dict suitable for JSON response.
    Handles nested dataclasses.
    """
    return dataclasses.asdict(report)
