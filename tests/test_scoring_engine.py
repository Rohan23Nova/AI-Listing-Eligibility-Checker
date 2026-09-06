"""
tests/test_scoring_engine.py — Unit tests for scoring_engine.

Tests cover:
  - Perfect score (all access + structure checks pass)
  - Zero score (everything blocked/missing)
  - Partial access (training blocked, search allowed)
  - LLM stub behavior (always returns "unavailable" with 0 pts in Checkpoint 1)
  - noindex override zeros structure scores
  - Issue list is sorted by points lost (most impactful first)
  - Verdict thresholds (Excellent ≥ 85%, Good ≥ 65%, Fair ≥ 40%, Poor < 40%)
"""

from __future__ import annotations

import pytest

from backend.models import (
    AccessResult,
    BotAccessResult,
    BotCategory,
    HeadingResult,
    JsonLdResult,
    LlmsTxtResult,
    MetaTagResult,
    SitemapResult,
    StructureResult,
    UASpoofResult,
    BOTS,
    TRAINING_BOTS,
    SEARCH_BOTS,
)
from backend.scoring_engine import (
    compute_score,
    score_access,
    score_structure,
    score_content_quality,
    score_citation_likelihood,
    W_ROBOTS_SEARCH_BOTS,
    W_ROBOTS_TRAINING_BOTS,
    W_UA_NO_BLOCK,
    W_SITEMAP,
    W_LLMS_TXT,
    W_JSON_LD,
    W_META_DESCRIPTION,
    W_OG_TAGS,
    W_HEADING_H1,
    W_HEADING_HIERARCHY,
    TOTAL_RULE_BASED,
)


# ── Fixture builders ──────────────────────────────────────────────────────────

def _make_access_result(
    all_allowed: bool = True,
    search_allowed: bool = True,
    training_allowed: bool = True,
    ua_blocked_uas: set[str] | None = None,
    sitemap: bool = True,
    llms_txt: bool = False,
) -> AccessResult:
    ua_blocked_uas = ua_blocked_uas or set()

    robots_per_bot = []
    for bot in BOTS:
        if bot.category == BotCategory.SEARCH:
            allowed = search_allowed if not all_allowed else True
        else:
            allowed = training_allowed if not all_allowed else True
        if all_allowed is False and search_allowed is False and training_allowed is False:
            allowed = False

        robots_per_bot.append(BotAccessResult(
            bot=bot,
            allowed=allowed,
            disallowed_paths=[] if allowed else ["/"],
            crawl_delay=None,
            rule_source="robots.txt",
        ))

    ua_spoof_results = [
        UASpoofResult(
            bot=bot,
            http_status=403 if bot.user_agent in ua_blocked_uas else 200,
            redirect_chain=[],
            blocked=bot.user_agent in ua_blocked_uas,
            response_size_bytes=1000,
            error=None,
        )
        for bot in BOTS
    ]

    return AccessResult(
        url="https://example.com",
        robots_per_bot=robots_per_bot,
        sitemap=SitemapResult(
            exists=sitemap,
            url="https://example.com/sitemap.xml" if sitemap else None,
            url_count=10 if sitemap else None,
            last_modified=None,
            source="default-path" if sitemap else "absent",
        ),
        llms_txt=LlmsTxtResult(
            exists=llms_txt,
            url="https://example.com/llms.txt",
            has_content=llms_txt,
        ),
        ua_spoof_results=ua_spoof_results,
        raw_evidence={},
    )


def _make_structure_result(
    json_ld: bool = True,
    json_ld_malformed: bool = False,
    meta_description: str | None = "A great description of this page.",
    og_tags: bool = True,
    h1: bool = True,
    multiple_h1: bool = False,
    hierarchy_issues: list[str] | None = None,
    noindex: bool = False,
) -> StructureResult:
    return StructureResult(
        url="https://example.com",
        json_ld=JsonLdResult(
            present=json_ld,
            schemas=[{"@type": "WebPage"}] if json_ld else [],
            types=["WebPage"] if json_ld else [],
            malformed=json_ld_malformed,
        ),
        meta=MetaTagResult(
            title="Example",
            description=meta_description,
            og_title="OG Title" if og_tags else None,
            og_description="OG Desc" if og_tags else None,
            og_image=None,
            canonical="https://example.com/",
            robots_meta="noindex" if noindex else None,
            noindex=noindex,
            noarchive=False,
        ),
        headings=HeadingResult(
            has_h1=h1,
            h1_count=2 if multiple_h1 else (1 if h1 else 0),
            heading_text=[("h1", "Main Title")] if h1 else [],
            hierarchy_issues=hierarchy_issues or [],
        ),
        raw_evidence={},
    )


# ── Access scoring tests ──────────────────────────────────────────────────────

def test_score_access_perfect():
    """All bots allowed, sitemap present → full access score."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    items = score_access(access)
    total = sum(i.points_earned for i in items)
    assert total == W_ROBOTS_SEARCH_BOTS + W_ROBOTS_TRAINING_BOTS + W_UA_NO_BLOCK + W_SITEMAP + W_LLMS_TXT


def test_score_access_search_bots_blocked():
    """All search bots blocked → 0 pts for search bot category."""
    access = _make_access_result(search_allowed=False, training_allowed=True, all_allowed=False)
    items = score_access(access)
    search_item = next(i for i in items if "Search" in i.label)
    assert search_item.points_earned == 0


def test_score_access_training_bots_blocked():
    """Training bots blocked → 0 pts for training category (not a penalty per spec)."""
    access = _make_access_result(search_allowed=True, training_allowed=False, all_allowed=False)
    items = score_access(access)
    training_item = next(i for i in items if "Training" in i.label)
    assert training_item.points_earned == 0
    # But search bots should still get full points
    search_item = next(i for i in items if "Search" in i.label)
    assert search_item.points_earned == W_ROBOTS_SEARCH_BOTS


def test_score_access_ua_blocked():
    """UA blocking reduces UA-spoof score."""
    blocked = {BOTS[0].user_agent, BOTS[1].user_agent}
    access = _make_access_result(ua_blocked_uas=blocked)
    items = score_access(access)
    ua_item = next(i for i in items if "UA-based" in i.label)
    assert ua_item.points_earned < W_UA_NO_BLOCK


def test_score_access_no_sitemap():
    access = _make_access_result(sitemap=False)
    items = score_access(access)
    sitemap_item = next(i for i in items if "sitemap" in i.label.lower())
    assert sitemap_item.points_earned == 0


def test_score_access_llms_txt_bonus_only():
    """llms.txt is worth exactly 1 pt — the spec calls it a bonus-only signal."""
    access_with = _make_access_result(llms_txt=True)
    access_without = _make_access_result(llms_txt=False)
    items_with = score_access(access_with)
    items_without = score_access(access_without)

    llms_with = next(i for i in items_with if "llms.txt" in i.label)
    llms_without = next(i for i in items_without if "llms.txt" in i.label)

    assert llms_with.points_earned == W_LLMS_TXT  # 1 pt
    assert llms_without.points_earned == 0
    # The reason must mention "bonus"
    assert "bonus" in llms_with.reason.lower()


# ── Structure scoring tests ───────────────────────────────────────────────────

def test_score_structure_perfect():
    """All structure checks pass → full structure score."""
    structure = _make_structure_result()
    items = score_structure(structure)
    total = sum(i.points_earned for i in items)
    expected = W_JSON_LD + W_META_DESCRIPTION + W_OG_TAGS + W_HEADING_H1 + W_HEADING_HIERARCHY
    assert total == expected


def test_score_structure_no_json_ld():
    structure = _make_structure_result(json_ld=False)
    items = score_structure(structure)
    json_ld_item = next(i for i in items if "JSON-LD" in i.label)
    assert json_ld_item.points_earned == 0


def test_score_structure_malformed_json_ld():
    """Malformed JSON-LD gets partial credit."""
    structure = _make_structure_result(json_ld=True, json_ld_malformed=True)
    items = score_structure(structure)
    json_ld_item = next(i for i in items if "JSON-LD" in i.label)
    assert 0 < json_ld_item.points_earned < W_JSON_LD


def test_score_structure_no_meta_description():
    structure = _make_structure_result(meta_description=None)
    items = score_structure(structure)
    meta_item = next(i for i in items if "Meta description" in i.label)
    assert meta_item.points_earned == 0


def test_score_structure_no_og_tags():
    structure = _make_structure_result(og_tags=False)
    items = score_structure(structure)
    og_item = next(i for i in items if "OpenGraph" in i.label)
    assert og_item.points_earned == 0


def test_score_structure_no_h1():
    structure = _make_structure_result(h1=False)
    items = score_structure(structure)
    h1_item = next(i for i in items if "H1" in i.label)
    assert h1_item.points_earned == 0


def test_score_structure_noindex_zeros_all():
    """noindex in meta → all structure points zeroed (AI crawlers won't index)."""
    structure = _make_structure_result(noindex=True)
    items = score_structure(structure)
    total = sum(i.points_earned for i in items)
    assert total == 0
    assert any("noindex" in i.reason.lower() for i in items)


def test_score_structure_hierarchy_issues():
    """Heading hierarchy issues reduce the hierarchy sub-score."""
    structure = _make_structure_result(
        hierarchy_issues=["H3 before H2 — broken heading hierarchy"]
    )
    items = score_structure(structure)
    hier_item = next(i for i in items if "Heading hierarchy" in i.label)
    assert hier_item.points_earned < W_HEADING_HIERARCHY


# ── LLM stubs ─────────────────────────────────────────────────────────────────

def test_llm_content_quality_stub_unavailable():
    """In Checkpoint 1, LLM call #1 should always return unavailable."""
    item = score_content_quality(None)
    assert item.points_earned == 0
    assert "unavailable" in item.reason.lower()


def test_llm_citation_stub_unavailable():
    """In Checkpoint 1, LLM call #2 should always return unavailable."""
    item = score_citation_likelihood(None)
    assert item.points_earned == 0
    assert "unavailable" in item.reason.lower()


# ── compute_score integration ─────────────────────────────────────────────────

def test_compute_score_perfect_deterministic():
    """Perfect rule-based score normalized to 100."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    structure = _make_structure_result()
    score = compute_score(access, structure)
    assert score.total == 100.0
    assert score.verdict == "Excellent"
    assert score.llm_available is False


def test_compute_score_zero():
    """All checks fail + all UAs blocked → lowest possible score."""
    all_ua_blocked = {bot.user_agent for bot in BOTS}
    access = _make_access_result(
        all_allowed=False, search_allowed=False, training_allowed=False,
        ua_blocked_uas=all_ua_blocked,
        sitemap=False, llms_txt=False,
    )
    structure = _make_structure_result(
        json_ld=False, meta_description=None, og_tags=False, h1=False,
        hierarchy_issues=["No headings"],
    )
    score = compute_score(access, structure)
    assert score.total < 5
    assert score.verdict == "Poor"


def test_compute_score_issues_sorted_by_impact():
    """Issues should be sorted highest-impact (most points lost) first."""
    access = _make_access_result(all_allowed=False, search_allowed=False, training_allowed=False, sitemap=False)
    structure = _make_structure_result(json_ld=False, meta_description=None)
    score = compute_score(access, structure)

    if len(score.issues) > 1:
        losses = [float(i["points_lost"]) for i in score.issues]
        assert losses == sorted(losses, reverse=True), "Issues not sorted by impact"


def test_compute_score_verdict_thresholds():
    """Check verdict boundaries."""
    # Pure rule-based: max is 70 pts, we normalize to 100
    # Excellent ≥ 85
    access_good = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    struct_good = _make_structure_result()
    assert compute_score(access_good, struct_good).verdict == "Excellent"

    # Poor < 40
    access_bad = _make_access_result(all_allowed=False, search_allowed=False, training_allowed=False, sitemap=False)
    struct_bad = _make_structure_result(json_ld=False, meta_description=None, og_tags=False, h1=False)
    assert compute_score(access_bad, struct_bad).verdict == "Poor"


def test_compute_score_raw_evidence_present():
    """Every score must have raw evidence — no black-box outputs."""
    access = _make_access_result()
    structure = _make_structure_result()
    score = compute_score(access, structure)

    for item in score.breakdown:
        assert item.evidence is not None, f"Missing evidence for: {item.label}"


# ── Eligibility Gate Tests ───────────────────────────────────────────────────

def test_eligibility_gate_poor_access():
    """1/5 search bots allowed -> <0.4 ratio -> score capped at 45."""
    # Force exactly 1 search bot to be allowed
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    # Modify robots_per_bot to block 4 out of 5 search bots
    search_uas = [b.user_agent for b in SEARCH_BOTS]
    for r in access.robots_per_bot:
        if r.bot.user_agent in search_uas[1:]:
            r.allowed = False
    
    structure = _make_structure_result()
    score = compute_score(access, structure)
    
    assert score.gate_applied is True
    assert score.gate_reason is not None
    assert "capped at 45" in score.gate_reason
    assert score.total <= 45.0
    assert score.verdict in ("Poor", "Fair")


def test_eligibility_gate_fair_access():
    """4/5 search bots allowed -> no cap, high score retained."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    search_uas = [b.user_agent for b in SEARCH_BOTS]
    for r in access.robots_per_bot:
        if r.bot.user_agent in search_uas[4:]:
            r.allowed = False
    
    structure = _make_structure_result()
    score = compute_score(access, structure)
    
    assert score.gate_applied is False
    assert score.gate_reason is None
    # 4/5 search bots allowed, excellent structure -> score should be high (>= 80)
    assert score.total > 80.0


def test_eligibility_gate_perfect_access():
    """5/5 search bots allowed -> no penalty."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    structure = _make_structure_result()
    score = compute_score(access, structure)
    
    assert score.gate_applied is False
    assert score.gate_reason is None
    assert score.total == 100.0


def test_eligibility_gate_boundary_exact_0_4():
    """Exactly 0.4 ratio (e.g. 2/5) should NOT trigger the <0.4 branch but SHOULD trigger <0.6."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    search_uas = [b.user_agent for b in SEARCH_BOTS]
    for r in access.robots_per_bot:
        if r.bot.user_agent in search_uas[2:]:
            r.allowed = False
            
    structure = _make_structure_result()
    score = compute_score(access, structure)
    
    assert score.gate_applied is True
    assert "reduced 15%" in score.gate_reason
    assert "capped at 45" not in score.gate_reason


def test_eligibility_gate_boundary_exact_0_6():
    """Exactly 0.6 ratio (e.g. 3/5) should NOT trigger <0.6 branch (no penalty)."""
    access = _make_access_result(all_allowed=True, sitemap=True, llms_txt=True)
    search_uas = [b.user_agent for b in SEARCH_BOTS]
    for r in access.robots_per_bot:
        if r.bot.user_agent in search_uas[3:]:
            r.allowed = False
            
    structure = _make_structure_result()
    score = compute_score(access, structure)
    
    assert score.gate_applied is False
    assert score.gate_reason is None
