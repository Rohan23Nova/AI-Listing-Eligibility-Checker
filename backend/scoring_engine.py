"""
scoring_engine.py — Explainable, weighted scoring of all checker results.

Scoring weights are defined ONCE here (see WEIGHTS block below) and used
consistently throughout. Every point gained or lost has a corresponding
ScoreBreakdownItem with a human-readable reason and the raw evidence that
determined it. No black-box scoring.

Weight design:
  ┌─────────────────────────────────────┬────────┐
  │ Category                            │ Weight │
  ├─────────────────────────────────────┼────────┤
  │ Access (robots, UA, sitemap)        │  35 pts│
  │ Structure (schema, meta, headings)  │  35 pts│
  │ Content quality  (LLM call #1)      │  15 pts│  ← stub in Checkpoint 1
  │ Citation likelihood (LLM call #2)   │  15 pts│  ← stub in Checkpoint 1
  └─────────────────────────────────────┴────────┘
  Total possible: 100 pts

  When LLM calls are unavailable, those 30 pts are excluded and the score is
  normalised to 70. The UI must clearly label this as "LLM scoring unavailable".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.models import (
    AccessResult,
    BotCategory,
    ContentResult,
    EmpiricalResult,
    ScoreBreakdownItem,
    ScoreResult,
    StructureResult,
    TRAINING_BOTS,
    SEARCH_BOTS,
)


# ── Weight constants (single source of truth) ─────────────────────────────────
# All sub-component weights must sum to their category total.

# Access (35 pts total)
W_ROBOTS_SEARCH_BOTS    = 15   # Search bots allowed in robots.txt (core signal)
W_ROBOTS_TRAINING_BOTS  =  5   # Training bots allowed (minor — site's choice)
W_UA_NO_BLOCK           = 10   # No bot UA blocked at HTTP level (empirical)
W_SITEMAP               =  4   # sitemap.xml present
W_LLMS_TXT              =  1   # /llms.txt present (bonus-only — see note below)
# ↑ llms.txt weight is kept at 1pt deliberately — it has no confirmed provider
#   adoption and must never be the reason a site passes or fails.

# Structure (35 pts total)
W_JSON_LD               = 12   # JSON-LD schema present and valid
W_META_DESCRIPTION      =  8   # Meta description present
W_OG_TAGS               =  5   # OG title + description present
W_HEADING_H1            =  5   # At least one H1 present
W_HEADING_HIERARCHY     =  5   # No heading hierarchy issues

# Content quality — LLM call #1 (15 pts total) — STUB IN CHECKPOINT 1
W_LLM_CONTENT_QUALITY   = 15

# Citation likelihood — LLM call #2 (15 pts total) — STUB IN CHECKPOINT 1
W_LLM_CITATION          = 15

TOTAL_RULE_BASED = (
    W_ROBOTS_SEARCH_BOTS + W_ROBOTS_TRAINING_BOTS + W_UA_NO_BLOCK +
    W_SITEMAP + W_LLMS_TXT +
    W_JSON_LD + W_META_DESCRIPTION + W_OG_TAGS + W_HEADING_H1 + W_HEADING_HIERARCHY
)  # = 70

TOTAL_LLM_BASED = W_LLM_CONTENT_QUALITY + W_LLM_CITATION  # = 30
TOTAL_MAX = TOTAL_RULE_BASED + TOTAL_LLM_BASED               # = 100


def _verdict(score: float, max_score: float) -> str:
    pct = score / max_score * 100 if max_score else 0
    if pct >= 85:
        return "Excellent"
    elif pct >= 65:
        return "Good"
    elif pct >= 40:
        return "Fair"
    else:
        return "Poor"


# ── Access scoring ────────────────────────────────────────────────────────────

def score_access(access: AccessResult) -> list[ScoreBreakdownItem]:
    """
    Score the access checker results.
    Returns a list of ScoreBreakdownItems, one per sub-component.
    """
    items: list[ScoreBreakdownItem] = []

    # --- Search bots in robots.txt (15 pts) ---
    search_bot_uas = {b.user_agent for b in SEARCH_BOTS}
    robots_by_ua = {r.bot.user_agent: r for r in access.robots_per_bot}

    search_allowed = [
        ua for ua in search_bot_uas
        if robots_by_ua.get(ua) and robots_by_ua[ua].allowed
    ]
    search_total = len(search_bot_uas)
    search_pts = round(W_ROBOTS_SEARCH_BOTS * len(search_allowed) / search_total, 1) if search_total else 0

    blocked_search = [ua for ua in search_bot_uas if ua not in [s for s in search_allowed]]
    items.append(ScoreBreakdownItem(
        label="Search/Answer bots allowed (robots.txt)",
        points_earned=search_pts,
        points_possible=W_ROBOTS_SEARCH_BOTS,
        reason=(
            f"{len(search_allowed)}/{search_total} search bots allowed in robots.txt"
            + (f". Blocked: {', '.join(blocked_search)}" if blocked_search else "")
        ),
        evidence={
            "allowed": search_allowed,
            "blocked": blocked_search,
            "per_bot": {
                ua: {"allowed": robots_by_ua[ua].allowed, "disallowed_paths": robots_by_ua[ua].disallowed_paths}
                for ua in search_bot_uas if ua in robots_by_ua
            },
        },
    ))

    # --- Training bots in robots.txt (5 pts) ---
    training_bot_uas = {b.user_agent for b in TRAINING_BOTS}
    training_allowed = [
        ua for ua in training_bot_uas
        if robots_by_ua.get(ua) and robots_by_ua[ua].allowed
    ]
    training_total = len(training_bot_uas)
    training_pts = round(W_ROBOTS_TRAINING_BOTS * len(training_allowed) / training_total, 1) if training_total else 0

    blocked_training = [ua for ua in training_bot_uas if ua not in [t for t in training_allowed]]
    items.append(ScoreBreakdownItem(
        label="Training bots allowed (robots.txt)",
        points_earned=training_pts,
        points_possible=W_ROBOTS_TRAINING_BOTS,
        reason=(
            f"{len(training_allowed)}/{training_total} training bots allowed. "
            "Note: blocking training bots is a site owner's legitimate choice "
            "and does not affect search/answer bot access."
            + (f" Blocked: {', '.join(blocked_training)}" if blocked_training else "")
        ),
        evidence={
            "allowed": training_allowed,
            "blocked": blocked_training,
        },
    ))

    # --- UA-level blocking (10 pts) ---
    # Deduct points for bots that get a non-200 response when fetched with their UA.
    # This catches sites that allow bots in robots.txt but block at the HTTP layer.
    ua_results_by_ua = {r.bot.user_agent: r for r in access.ua_spoof_results}
    ua_blocked_bots = [
        r.bot.user_agent for r in access.ua_spoof_results
        if r.blocked or (r.error is not None)
    ]
    ua_blocked_search = [ua for ua in ua_blocked_bots if ua in search_bot_uas]
    ua_blocked_training = [ua for ua in ua_blocked_bots if ua in training_bot_uas]

    total_bots = len(access.ua_spoof_results)
    not_blocked = total_bots - len(ua_blocked_bots)
    ua_pts = round(W_UA_NO_BLOCK * not_blocked / total_bots, 1) if total_bots else 0

    items.append(ScoreBreakdownItem(
        label="No UA-based HTTP blocking (empirical)",
        points_earned=ua_pts,
        points_possible=W_UA_NO_BLOCK,
        reason=(
            f"{not_blocked}/{total_bots} bots received non-blocking HTTP response"
            + (f". HTTP-blocked search bots: {', '.join(ua_blocked_search)}" if ua_blocked_search else "")
            + (f". HTTP-blocked training bots: {', '.join(ua_blocked_training)}" if ua_blocked_training else "")
        ),
        evidence={
            "blocked": ua_blocked_bots,
            "blocked_search": ua_blocked_search,
            "blocked_training": ua_blocked_training,
            "per_bot": {
                r.bot.user_agent: {
                    "status": r.http_status,
                    "blocked": r.blocked,
                    "error": r.error,
                }
                for r in access.ua_spoof_results
            },
        },
    ))

    # --- Sitemap (4 pts) ---
    sitemap_pts = W_SITEMAP if access.sitemap.exists else 0
    items.append(ScoreBreakdownItem(
        label="sitemap.xml present",
        points_earned=sitemap_pts,
        points_possible=W_SITEMAP,
        reason=(
            f"sitemap.xml found at {access.sitemap.url} ({access.sitemap.url_count} URLs, source: {access.sitemap.source})"
            if access.sitemap.exists
            else "No sitemap.xml found — AI crawlers cannot efficiently discover all pages"
        ),
        evidence={
            "exists": access.sitemap.exists,
            "url": access.sitemap.url,
            "url_count": access.sitemap.url_count,
            "source": access.sitemap.source,
        },
    ))

    # --- llms.txt (1 pt bonus-only) ---
    llms_pts = W_LLMS_TXT if access.llms_txt.exists else 0
    items.append(ScoreBreakdownItem(
        label="llms.txt present (bonus signal only)",
        points_earned=llms_pts,
        points_possible=W_LLMS_TXT,
        reason=(
            (f"llms.txt found at {access.llms_txt.url}. "
             "IMPORTANT: This is a bonus signal only — llms.txt has no standards-body "
             "backing and no confirmed adoption by any major AI provider.")
            if access.llms_txt.exists
            else "No llms.txt found. Not a penalty — this is a bonus-only signal."
        ),
        evidence={
            "exists": access.llms_txt.exists,
            "url": access.llms_txt.url,
            "note": access.llms_txt.note,
        },
    ))

    return items


# ── Structure scoring ─────────────────────────────────────────────────────────

def score_structure(structure: StructureResult) -> list[ScoreBreakdownItem]:
    """
    Score the structure checker results.
    Returns a list of ScoreBreakdownItems.
    """
    items: list[ScoreBreakdownItem] = []

    # Noindex override: if the page has noindex, cap structure score at 0
    # because AI crawlers will not index the page regardless of other signals.
    if structure.meta.noindex:
        items.append(ScoreBreakdownItem(
            label="noindex override — all structure points zeroed",
            points_earned=0,
            points_possible=TOTAL_RULE_BASED - W_ROBOTS_SEARCH_BOTS - W_ROBOTS_TRAINING_BOTS - W_UA_NO_BLOCK - W_SITEMAP - W_LLMS_TXT,
            reason=(
                f'<meta name="robots" content="{structure.meta.robots_meta}"> '
                "contains noindex. AI crawlers that respect meta-robots (most do) "
                "will not index this page regardless of other signals."
            ),
            evidence={"robots_meta": structure.meta.robots_meta},
        ))
        return items

    # --- JSON-LD (12 pts) ---
    if structure.json_ld.present and not structure.json_ld.malformed:
        json_ld_pts = W_JSON_LD
        reason = f"Valid JSON-LD found. Types: {', '.join(structure.json_ld.types) or 'untyped'}"
    elif structure.json_ld.present and structure.json_ld.malformed:
        json_ld_pts = W_JSON_LD // 2  # partial credit for presence even if malformed
        reason = "JSON-LD found but contains parse errors. AI crawlers may ignore malformed schema."
    else:
        json_ld_pts = 0
        reason = "No JSON-LD schema found. Structured data helps AI systems understand page context."

    items.append(ScoreBreakdownItem(
        label="JSON-LD structured data",
        points_earned=json_ld_pts,
        points_possible=W_JSON_LD,
        reason=reason,
        evidence={
            "present": structure.json_ld.present,
            "types": structure.json_ld.types,
            "malformed": structure.json_ld.malformed,
            "schema_count": len(structure.json_ld.schemas),
        },
    ))

    # --- Meta description (8 pts) ---
    if structure.meta.description and len(structure.meta.description.strip()) > 10:
        meta_pts = W_META_DESCRIPTION
        reason = f"Meta description present ({len(structure.meta.description)} chars)"
    elif structure.meta.description:
        meta_pts = W_META_DESCRIPTION // 2
        reason = "Meta description present but very short — consider a more descriptive summary"
    else:
        meta_pts = 0
        reason = "No meta description — AI systems use this as a primary content summary signal"

    items.append(ScoreBreakdownItem(
        label="Meta description",
        points_earned=meta_pts,
        points_possible=W_META_DESCRIPTION,
        reason=reason,
        evidence={"description": structure.meta.description},
    ))

    # --- OG tags (5 pts) ---
    has_og_title = bool(structure.meta.og_title)
    has_og_desc = bool(structure.meta.og_description)
    if has_og_title and has_og_desc:
        og_pts = W_OG_TAGS
        reason = "OpenGraph og:title and og:description both present"
    elif has_og_title or has_og_desc:
        og_pts = W_OG_TAGS // 2
        reason = f"Partial OG tags — {'og:title' if has_og_title else 'og:description'} present but not both"
    else:
        og_pts = 0
        reason = "No OpenGraph tags — OG metadata helps AI answer engines surface rich previews"

    items.append(ScoreBreakdownItem(
        label="OpenGraph tags",
        points_earned=og_pts,
        points_possible=W_OG_TAGS,
        reason=reason,
        evidence={"og_title": structure.meta.og_title, "og_description": structure.meta.og_description},
    ))

    # --- H1 heading (5 pts) ---
    if structure.headings.has_h1 and structure.headings.h1_count == 1:
        h1_pts = W_HEADING_H1
        reason = "Exactly one H1 found — strong primary topic signal for AI chunkers"
    elif structure.headings.has_h1:
        h1_pts = W_HEADING_H1 // 2
        reason = f"{structure.headings.h1_count} H1 tags found — should have exactly one H1 per page"
    else:
        h1_pts = 0
        reason = "No H1 tag — AI crawlers rely on H1 as the primary topic signal"

    items.append(ScoreBreakdownItem(
        label="H1 heading present",
        points_earned=h1_pts,
        points_possible=W_HEADING_H1,
        reason=reason,
        evidence={"h1_count": structure.headings.h1_count},
    ))

    # --- Heading hierarchy (5 pts) ---
    if not structure.headings.hierarchy_issues:
        hier_pts = W_HEADING_HIERARCHY
        reason = "Heading hierarchy is well-structured (no H1→H2→H3 skips)"
    else:
        # Deduct per issue (min 0)
        deduction = min(len(structure.headings.hierarchy_issues) * 2, W_HEADING_HIERARCHY)
        hier_pts = max(0, W_HEADING_HIERARCHY - deduction)
        reason = f"Heading hierarchy issues: {'; '.join(structure.headings.hierarchy_issues)}"

    items.append(ScoreBreakdownItem(
        label="Heading hierarchy",
        points_earned=hier_pts,
        points_possible=W_HEADING_HIERARCHY,
        reason=reason,
        evidence={"issues": structure.headings.hierarchy_issues},
    ))

    return items


# ── LLM stub scoring ──────────────────────────────────────────────────────────
# These will be replaced with real LLM calls in M8 (Checkpoint 3).
# For now they return 0 pts with a clear "unavailable" label.

def score_content_quality(content: ContentResult | None) -> ScoreBreakdownItem:
    """LLM call #1 stub — content quality judgment."""
    if content and content.llm_quality_score is not None:
        pts = round(content.llm_quality_score * W_LLM_CONTENT_QUALITY, 1)
        return ScoreBreakdownItem(
            label="Content quality (LLM judgment)",
            points_earned=pts,
            points_possible=W_LLM_CONTENT_QUALITY,
            reason=f"LLM quality score: {content.llm_quality_label}. {content.llm_quality_reasoning or ''}",
            evidence={"score": content.llm_quality_score, "label": content.llm_quality_label},
        )
    return ScoreBreakdownItem(
        label="Content quality (LLM judgment)",
        points_earned=0,
        points_possible=W_LLM_CONTENT_QUALITY,
        reason="LLM scoring unavailable — GROQ_API_KEY or GEMINI_API_KEY not configured, or ENABLE_LLM=false",
        evidence={"status": "unavailable"},
    )


def score_citation_likelihood(empirical: EmpiricalResult | None) -> ScoreBreakdownItem:
    """LLM call #2 stub — citation likelihood judgment."""
    if empirical and empirical.llm_citation_score is not None:
        pts = round(empirical.llm_citation_score * W_LLM_CITATION, 1)
        return ScoreBreakdownItem(
            label="Citation likelihood (LLM judgment)",
            points_earned=pts,
            points_possible=W_LLM_CITATION,
            reason=f"LLM citation score: {empirical.llm_citation_label}. {empirical.llm_citation_reasoning or ''}",
            evidence={"score": empirical.llm_citation_score, "label": empirical.llm_citation_label},
        )
    return ScoreBreakdownItem(
        label="Citation likelihood (LLM judgment)",
        points_earned=0,
        points_possible=W_LLM_CITATION,
        reason="LLM scoring unavailable — GROQ_API_KEY or GEMINI_API_KEY not configured, or ENABLE_LLM=false",
        evidence={"status": "unavailable"},
    )


# ── Issue extractor ───────────────────────────────────────────────────────────

def _extract_issues(breakdown: list[ScoreBreakdownItem]) -> list[dict[str, str]]:
    """
    Derive a prioritized list of actionable issues from score breakdowns.
    Issues are ordered by points lost (most impactful first).
    """
    issues = []
    for item in breakdown:
        lost = item.points_possible - item.points_earned
        if lost <= 0:
            continue
        # Severity tiers
        if lost >= 10:
            severity = "critical"
        elif lost >= 5:
            severity = "high"
        elif lost >= 2:
            severity = "medium"
        else:
            severity = "low"

        issues.append({
            "severity": severity,
            "title": item.label,
            "detail": item.reason,
            "points_lost": str(lost),
            "fix": _suggest_fix(item.label),
        })

    # Sort by points lost descending
    issues.sort(key=lambda x: float(x["points_lost"]), reverse=True)
    return issues


def _suggest_fix(label: str) -> str:
    """Return a brief, actionable fix suggestion for a given check label."""
    fixes = {
        "Search/Answer bots allowed (robots.txt)": (
            "Remove or restrict Disallow rules for OAI-SearchBot, PerplexityBot, "
            "Claude-SearchBot, Applebot-Extended, and Googlebot."
        ),
        "Training bots allowed (robots.txt)": (
            "If you want training data crawlers to access your site, remove Disallow "
            "rules for GPTBot, ClaudeBot, Google-Extended, and CCBot."
        ),
        "No UA-based HTTP blocking (empirical)": (
            "Your server appears to return non-200 responses to bot user-agents. "
            "Check WAF/CDN rules, rate-limiting middleware, and Cloudflare Bot Fight mode."
        ),
        "sitemap.xml present": (
            "Create a sitemap.xml at your domain root and reference it in robots.txt "
            "with a Sitemap: directive. Most CMS platforms have plugins for this."
        ),
        "JSON-LD structured data": (
            "Add <script type='application/ld+json'> with Article, WebPage, or "
            "Organization schema. Use schema.org vocabulary."
        ),
        "Meta description": (
            "Add <meta name='description' content='...'> with a 120-160 character "
            "summary of the page content."
        ),
        "OpenGraph tags": (
            "Add <meta property='og:title'> and <meta property='og:description'> "
            "to your page <head>."
        ),
        "H1 heading present": (
            "Add exactly one <h1> tag per page containing the primary topic. "
            "Every page should have one H1."
        ),
        "Heading hierarchy": (
            "Ensure headings follow logical order: H1 → H2 → H3. "
            "Avoid skipping levels (e.g., H1 directly to H3)."
        ),
    }
    return fixes.get(label, "See the fix-pack download for a template.")


# ── Main scoring function ─────────────────────────────────────────────────────

def compute_score(
    access: AccessResult,
    structure: StructureResult,
    content: ContentResult | None = None,
    empirical: EmpiricalResult | None = None,
) -> ScoreResult:
    """
    Compute the final score from all checker results.
    Every point is explainable via the breakdown list.
    """
    access_items = score_access(access)
    structure_items = score_structure(structure)
    content_item = score_content_quality(content)
    citation_item = score_citation_likelihood(empirical)

    all_items = access_items + structure_items + [content_item, citation_item]

    # Determine if LLM scoring contributed
    llm_available = (
        content_item.points_earned > 0 or citation_item.points_earned > 0
    )

    total_earned = sum(i.points_earned for i in all_items)
    llm_pts_possible = W_LLM_CONTENT_QUALITY + W_LLM_CITATION

    if not llm_available:
        # Normalise: express score out of 70 rule-based points, scale to 100
        rule_earned = sum(i.points_earned for i in access_items + structure_items)
        max_possible = float(TOTAL_RULE_BASED)
        total_score = round(rule_earned / TOTAL_RULE_BASED * 100, 1)
    else:
        max_possible = float(TOTAL_MAX)
        total_score = round(total_earned, 1)

    issues = _extract_issues(all_items)

    return ScoreResult(
        total=total_score,
        max_possible=max_possible,
        verdict=_verdict(total_score, 100),
        breakdown=all_items,
        issues=issues,
        llm_available=llm_available,
    )
