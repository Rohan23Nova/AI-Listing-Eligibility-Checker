#!/usr/bin/env python3
"""
scripts/demo.py — Before/After demo for Checkpoint 4.

Demonstrates the fix-pack workflow:
  1. Analyze a URL with known fixable issues (example.com)
  2. Show the score and issues
  3. Download the fix-pack (shown as text output)
  4. Simulate "after applying fixes" by analyzing a richer URL
  5. Show the score delta

Run: PYTHONPATH=. .venv/bin/python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.checkers.access_checker import run_access_checker
from backend.checkers.structure_checker import run_structure_checker
from backend.checkers.content_checker import run_content_checker
from backend.checkers.empirical_tester import run_empirical_tester
from backend.scoring_engine import compute_score
from backend.report_generator import generate_report, report_to_dict


async def analyze(url: str, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  {label}: {url}")
    print(f"{'='*60}")

    access = await run_access_checker(url)
    structure = await run_structure_checker(url)
    content = await run_content_checker(url)
    empirical = await run_empirical_tester(
        url=url,
        ua_results=access.ua_spoof_results,
        meta_description=structure.meta.description,
        json_ld_types=structure.json_ld.types,
        word_count=content.body_word_count,
        search_bots_allowed=sum(
            1 for r in access.robots_per_bot
            if r.allowed and r.bot.category.value == "search"
        ),
        search_bots_total=5,
    )

    score = compute_score(
        access=access,
        structure=structure,
        content=content,
        empirical=empirical,
    )
    report = generate_report(url, access, structure, score, content, empirical, 0)
    d = report_to_dict(report)

    total = d["score"]["total"]
    verdict = d["score"]["verdict"]
    print(f"\n  Score: {total:.1f} / 100  —  {verdict}")
    print(f"  Search bots: {d['search_bots_summary']}")
    print(f"  Training bots: {d['training_bots_summary']}")
    print(f"  Content words: {content.body_word_count}")

    print("\n  Top issues:")
    for issue in d["score"]["issues"][:5]:
        pts = issue["points_lost"]
        sev = issue["severity"].upper()
        print(f"    [{sev}] -{pts}pt  {issue['title']}")
        print(f"           {issue['detail'][:80]}...")

    if d.get("fix_pack"):
        print(f"\n  Fix-pack ({len(d['fix_pack'])} items):")
        for item in d["fix_pack"]:
            print(f"    • {item['filename']} — {item['description']}")

    return d


async def main():
    print("\n" + "🤖 " * 20)
    print("AI LISTING ELIGIBILITY CHECKER — Before/After Demo")
    print("🤖 " * 20)

    # BEFORE: bare site with no structured data
    before = await analyze("https://example.com", "BEFORE (bare site)")

    print("\n" + "-" * 60)
    print("  [Applying fix-pack: adding JSON-LD, meta description, OG tags]")
    print("  [In a real scenario you'd apply these to your site, then re-run]")
    print("-" * 60)

    # AFTER: a well-structured site as the "fixed" comparison
    after = await analyze("https://en.wikipedia.org/wiki/Artificial_intelligence", "AFTER reference (well-structured)")

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    before_score = before["score"]["total"]
    after_score = after["score"]["total"]
    delta = after_score - before_score
    print(f"  Before:  {before_score:.1f}  ({before['score']['verdict']})")
    print(f"  After:   {after_score:.1f}  ({after['score']['verdict']})")
    print(f"  Delta:   +{delta:.1f} pts")
    print("\n  The history panel in the UI shows this sparkline automatically")
    print("  when the same URL is re-analyzed after applying fixes.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
