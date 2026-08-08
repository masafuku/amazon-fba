"""Generic local SQLite cache for Keepa API responses.

Keepa charges tokens per request and this project's plan may only generate
1 token/minute, so avoiding redundant live calls matters. This is a plain
key/value cache with a per-entry timestamp; callers decide the TTL and
whether to bypass it (force_refresh), since freshness needs differ by data
type (prices/ranks change often, category trees barely change at all).

Not thread-safe beyond SQLite's own locking; fine for a single-process
stdio MCP server.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Tuple

DB_PATH = Path(__file__).resolve().parent / "cache.sqlite3"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keepa_cache (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (kind, key)
        )
        """
    )
    return conn


def get(kind: str, key: str, ttl_seconds: float) -> Optional[Tuple[Any, float]]:
    """Return (value, age_seconds) for a fresh entry, or None on miss/expiry."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value, fetched_at FROM keepa_cache WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value_json, fetched_at = row
    age = time.time() - fetched_at
    if ttl_seconds is not None and age > ttl_seconds:
        return None
    return json.loads(value_json), age


def set(kind: str, key: str, value: Any) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO keepa_cache (kind, key, value, fetched_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at
            """,
            (kind, key, json.dumps(value), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear(kind: Optional[str] = None) -> int:
    """Delete cache entries. Pass a kind to clear only that category. Returns row count deleted."""
    conn = _connect()
    try:
        if kind:
            cur = conn.execute("DELETE FROM keepa_cache WHERE kind = ?", (kind,))
        else:
            cur = conn.execute("DELETE FROM keepa_cache")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stats() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM keepa_cache GROUP BY kind"
        ).fetchall()
    finally:
        conn.close()
    now = time.time()
    return {
        kind: {
            "entries": count,
            "oldest_age_seconds": round(now - oldest, 1),
            "newest_age_seconds": round(now - newest, 1),
        }
        for kind, count, oldest, newest in rows
    }
