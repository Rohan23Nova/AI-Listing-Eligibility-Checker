"""
history_store.py — SQLite-backed score history per URL.

Schema: one row per (url, timestamp, score) analysis run.
Used for the before/after demo: re-running a URL after applying fixes
shows a visible score improvement in the history view.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from backend.config import SQLITE_DB_PATH


async def _get_db_path() -> str:
    path = Path(SQLITE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


async def init_db() -> None:
    """Create the history table if it doesn't exist."""
    db_path = await _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                score       REAL NOT NULL,
                verdict     TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_url ON history(url)")
        await db.commit()


async def save_report(url: str, score: float, verdict: str, report_dict: dict[str, Any]) -> int:
    """Save a report to the history table. Returns the new row ID."""
    db_path = await _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO history (url, timestamp, score, verdict, report_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                url,
                datetime.now(timezone.utc).isoformat(),
                score,
                verdict,
                json.dumps(report_dict),
            ),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def get_history(url: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent analysis runs for a URL, newest first."""
    db_path = await _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, url, timestamp, score, verdict
            FROM history
            WHERE url = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (url, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_all_urls(limit: int = 100) -> list[dict[str, Any]]:
    """Return distinct URLs that have been analyzed, with their latest score."""
    db_path = await _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT url, MAX(timestamp) as latest_timestamp, score as latest_score, verdict as latest_verdict
            FROM history
            GROUP BY url
            ORDER BY latest_timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
