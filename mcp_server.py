#!/usr/bin/env python3
"""MCP stdio server wrapping the local mem0 REST API."""

import datetime
import hashlib
import json
import math
import os
import sqlite3
import logging
import time

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP

MEM0_URL = os.getenv("MEM0_URL", "http://localhost:8000").rstrip("/")
USER_ID = os.getenv("MEM0_USER_ID", "default_user")

# Exact-content dedupe: identical adds within the window are skipped before
# they reach mem0 (saves a local-LLM extraction call per duplicate).
DEDUPE_DB = os.getenv("DEDUPE_DB", "/data/dedupe.db")
DEDUPE_WINDOW_SECONDS = int(os.getenv("DEDUPE_WINDOW_SECONDS", str(7 * 24 * 3600)))

mcp = FastMCP(
    "mem0",
    host=os.getenv("FASTMCP_HOST", "0.0.0.0"),
    port=int(os.getenv("FASTMCP_PORT", "8001")),
)


# Writes hit mem0's extraction LLM; a cold model load on the backend can
# stall the request for minutes, and a client timeout mid-add still commits
# server-side (retries then duplicate the memory) — so wait it out.
WRITE_TIMEOUT_SECONDS = int(os.getenv("WRITE_TIMEOUT_SECONDS", "300"))

MENTION_DEBOUNCE_SECONDS = int(os.getenv("MENTION_DEBOUNCE_SECONDS", str(6 * 3600)))
SEARCH_OVERFETCH = int(os.getenv("SEARCH_OVERFETCH", "50"))
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
MENTION_ALPHA = float(os.getenv("MENTION_ALPHA", "0.3"))
RECENCY_HALFLIFE_DAYS = float(os.getenv("RECENCY_HALFLIFE_DAYS", "30"))

# mem0's /memories response can't be relied on to signal reinforcement:
# near-duplicate content it classifies as "no update needed" is silently
# omitted from the results array entirely (no event, no id — confirmed
# against a live server), and a genuine "UPDATE" event only fires for a
# narrow LLM judgment call that's rare in practice. A pre-add similarity
# search is the only reliable way to get a candidate memory_id to bump.
REINFORCEMENT_THRESHOLD = float(os.getenv("REINFORCEMENT_THRESHOLD", "0.85"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DEDUPE_DB, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY, ts REAL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mention_counts "
        "(memory_id TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_bumped REAL DEFAULT 0)"
    )
    return conn


def _is_duplicate_add(content: str) -> bool:
    digest = hashlib.sha256(f"{USER_ID}|{content.strip()}".encode()).hexdigest()
    now = time.time()
    try:
        conn = _db()
    except sqlite3.OperationalError:
        return False  # dedupe store unavailable — never block a write
    try:
        conn.execute("DELETE FROM seen WHERE ts < ?", (now - DEDUPE_WINDOW_SECONDS,))
        row = conn.execute("SELECT 1 FROM seen WHERE hash = ?", (digest,)).fetchone()
        if row:
            conn.commit()
            return True
        conn.execute("INSERT INTO seen (hash, ts) VALUES (?, ?)", (digest, now))
        conn.commit()
        return False
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _bump_mention(memory_id: str) -> None:
    """Increment mention count, at most once per debounce window."""
    now = time.time()
    try:
        conn = _db()
        row = conn.execute(
            "SELECT last_bumped FROM mention_counts WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mention_counts (memory_id, count, last_bumped) VALUES (?, 1, ?)",
                (memory_id, now),
            )
        elif now - row[0] >= MENTION_DEBOUNCE_SECONDS:
            conn.execute(
                "UPDATE mention_counts SET count = count + 1, last_bumped = ? WHERE memory_id = ?",
                (now, memory_id),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def _check_reinforcement(content: str) -> None:
    """Search for a highly-similar existing memory and bump it if found.

    Best-effort: runs before the add itself, never blocks or fails a write.
    """
    try:
        resp = httpx.post(
            f"{MEM0_URL}/search",
            json={"query": content, "filters": {"user_id": USER_ID}, "top_k": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data if isinstance(data, list) else data.get("results", [])
        if results and results[0].get("id") and float(results[0].get("score", 0)) >= REINFORCEMENT_THRESHOLD:
            _bump_mention(results[0]["id"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass


def _get_mention_counts(memory_ids: list) -> dict:
    """Return {memory_id: count} for the given IDs."""
    if not memory_ids:
        return {}
    try:
        conn = _db()
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, count FROM mention_counts WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        return {}


def _rerank(results: list, top_k: int) -> list:
    """Sort results by similarity × mention_boost × recency, return top_k."""
    now = datetime.datetime.now(datetime.timezone.utc)
    counts = _get_mention_counts([r.get("id", "") for r in results])
    scored = []
    for r in results:
        sim = float(r.get("score", 0.5))
        mentions = counts.get(r.get("id", ""), 0)
        mention_boost = 1 + MENTION_ALPHA * math.log1p(mentions)
        updated = r.get("updated_at") or r.get("created_at")
        try:
            dt = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))
            days = max(0, (now - dt).days)
            # floor at 0.5 so old memories don't fall off
            recency = 0.5 + 0.5 * math.exp(-days / RECENCY_HALFLIFE_DAYS)
        except (AttributeError, ValueError, TypeError):
            recency = 0.75
        scored.append((sim * mention_boost * recency, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:top_k]]


@mcp.tool()
def add_memory(content: str) -> str:
    """Store a memory in mem0. Pass the raw text you want to remember."""
    if _is_duplicate_add(content):
        return "Skipped: identical content was already stored within the last 7 days."
    _check_reinforcement(content)
    _t0 = time.monotonic()
    resp = httpx.post(
        f"{MEM0_URL}/memories",
        json={"messages": [{"role": "user", "content": content}], "user_id": USER_ID},
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    _elapsed = time.monotonic() - _t0
    if _elapsed > 5:
        logging.warning("add_memory: slow POST /memories — %.1fs", _elapsed)
    else:
        logging.info("add_memory: POST /memories completed in %.1fs", _elapsed)
    resp.raise_for_status()
    data = resp.json()
    # Fallback: bump mention count if mem0 ever does return UPDATE/NONE
    # directly (observed in practice: it doesn't, see _check_reinforcement
    # above, but this is free/harmless to leave in if that changes).
    for result in (data if isinstance(data, list) else data.get("results", [])):
        if result.get("event") in ("UPDATE", "NONE") and result.get("id"):
            _bump_mention(result["id"])
    return json.dumps(data)


@mcp.tool()
def search_memories(query: str) -> str:
    """Search memories in mem0 using semantic similarity."""
    resp = httpx.post(
        f"{MEM0_URL}/search",
        json={"query": query, "filters": {"user_id": USER_ID}, "top_k": SEARCH_OVERFETCH},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data if isinstance(data, list) else data.get("results", [])
    if not results:
        return "No memories found."
    results = _rerank(results, SEARCH_TOP_K)
    return "\n\n".join(
        f"[{r.get('id', '?')}] {r.get('memory', r.get('text', str(r)))}"
        for r in results
    )


@mcp.tool()
def get_all_memories() -> str:
    """List all stored memories."""
    # GET /memories hard-caps at 20 and ignores pagination params, so
    # enumerate via search with a zero threshold instead.
    resp = httpx.post(
        f"{MEM0_URL}/search",
        json={
            "query": "memory",
            "filters": {"user_id": USER_ID},
            "threshold": 0.0,
            "top_k": 500,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    memories = data if isinstance(data, list) else data.get("results", [])
    if not memories:
        return "No memories stored."
    memories = sorted(memories, key=lambda m: m.get("created_at") or "")
    return "\n\n".join(
        f"[{m.get('id', '?')}] {m.get('memory', m.get('text', str(m)))}"
        for m in memories
    )


@mcp.tool()
def update_memory(memory_id: str, content: str) -> str:
    """Replace the text of an existing memory, keeping its ID."""
    resp = httpx.put(
        f"{MEM0_URL}/memories/{memory_id}",
        json={"text": content},
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return f"Updated memory {memory_id}."


@mcp.tool()
def delete_memory(memory_id: str) -> str:
    """Delete a specific memory by its ID."""
    resp = httpx.delete(f"{MEM0_URL}/memories/{memory_id}", timeout=30)
    resp.raise_for_status()
    return f"Deleted memory {memory_id}."


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport == "streamable-http":
        app = mcp.streamable_http_app()
        uvicorn.run(
            app,
            host=os.getenv("FASTMCP_HOST", "0.0.0.0"),
            port=int(os.getenv("FASTMCP_PORT", "8001")),
        )
    else:
        mcp.run(transport=transport)
