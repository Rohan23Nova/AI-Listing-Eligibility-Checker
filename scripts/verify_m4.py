#!/usr/bin/env python3
"""
M4 real URL verification script.
Runs outside the web server — calls the checker modules directly with real network.
"""
import asyncio
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.checkers.access_checker import run_access_checker
from backend.checkers.structure_checker import run_structure_checker
from backend.scoring_engine import compute_score
from backend.report_generator import generate_report, report_to_dict


async def analyze(url: str) -> dict:
    print(f"\n{'='*60}")
    print(f"Analyzing: {url}")
    print('='*60)

    access, structure = await asyncio.gather(
        run_access_checker(url),
        run_structure_checker(url),
    )

    score = compute_score(access, structure)
    report = generate_report(url, access, structure, score)

    print(f"Score: {score.total:.1f}/100 — {score.verdict}")
    print(f"Training bots: {report.training_bots_summary}")
    print(f"Search bots:   {report.search_bots_summary}")
    print(f"Sitemap:  {'YES' if access.sitemap.exists else 'NO'} ({access.sitemap.source})")
    print(f"llms.txt: {'YES' if access.llms_txt.exists else 'NO'}")
    print(f"JSON-LD:  {'YES (' + ', '.join(structure.json_ld.types) + ')' if structure.json_ld.present else 'NO'}")
    print(f"noindex:  {'YES ⚠' if structure.meta.noindex else 'NO'}")

    print("\n--- Score Breakdown ---")
    for item in score.breakdown:
        earned = item.points_earned
        possible = item.points_possible
        bar = '█' * int(earned/possible*10) + '░' * (10 - int(earned/possible*10)) if possible else '░'*10
        print(f"  {bar} {earned:4.1f}/{possible:2} {item.label}")

    print("\n--- Per-Bot Table ---")
    print(f"  {'Bot':<22} {'Category':<10} {'robots':<8} {'UA status':<12} {'UA blocked'}")
    print(f"  {'-'*22} {'-'*10} {'-'*8} {'-'*12} {'-'*10}")
    for row in report.per_bot_table:
        robots = 'Allow' if row.robots_allowed else 'BLOCK'
        ua_s = str(row.ua_http_status) if row.ua_http_status else 'timeout'
        blocked = '⚠ BLOCKED' if row.ua_blocked else 'ok'
        print(f"  {row.bot_name:<22} {row.category:<10} {robots:<8} {ua_s:<12} {blocked}")

    print("\n--- Top Issues ---")
    for issue in score.issues[:5]:
        print(f"  [{issue['severity'].upper():8}] (-{issue['points_lost']:4} pts) {issue['title']}")

    if report.fix_pack:
        print(f"\n--- Fix Pack ({len(report.fix_pack)} files) ---")
        for fp in report.fix_pack:
            print(f"  {fp.filename}: {fp.description[:70]}")

    return report_to_dict(report)


async def main():
    test_urls = [
        "https://openai.com",
        "https://nytimes.com",
        "https://en.wikipedia.org/wiki/Artificial_intelligence",
        "https://example.com",
    ]

    results = []
    for url in test_urls:
        try:
            result = await analyze(url)
            results.append({"url": url, "score": result["score"]["total"], "verdict": result["score"]["verdict"]})
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"url": url, "error": str(e)})

    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print('='*60)
    for r in results:
        if "error" in r:
            print(f"  {r['url']}: ERROR — {r['error']}")
        else:
            bar = '█' * int(r['score'] / 10)
            print(f"  {r['score']:5.1f} {bar:<10} {r['verdict']:<12} {r['url']}")


if __name__ == "__main__":
    asyncio.run(main())
