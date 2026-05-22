"""
SQLite persistence for larksor-tc.

Replaces the old single-file state.json with a small SQLite database
that also captures per-turn history, token usage and file includes —
enabling /cost /history /ls /resume commands and future analytics.

Schema is intentionally tiny so it can evolve without migrations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path.home() / "larksor-tc"
DB_PATH = ROOT / "state.db"
LEGACY_STATE_FILE = ROOT / "state.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (julianday('now'))
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (julianday('now')),
    last_used_at REAL NOT NULL DEFAULT (julianday('now')),
    model TEXT,
    workspace TEXT,
    title TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chats_open_id ON chats(open_id);
CREATE INDEX IF NOT EXISTS idx_chats_last_used ON chats(last_used_at DESC);

CREATE TABLE IF NOT EXISTS turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    open_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    model TEXT,
    mode TEXT,
    prompt TEXT,
    result TEXT,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    card_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_turns_open_id_started ON turns(open_id, started_at DESC);

CREATE TABLE IF NOT EXISTS includes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_includes_turn ON includes(turn_id);
"""

# sqlite3 connections aren't safely shared across threads by default; we lock.
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def init(path: Path = DB_PATH) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), check_same_thread=False,
                            isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)
        _conn = c
        _migrate_from_legacy_json()
        return _conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        return init()
    return _conn


# ---------------------------------------------------------------------------
# KV store - replaces state.json
# ---------------------------------------------------------------------------

def kv_set(key: str, value: Any) -> None:
    if value is None:
        kv_delete(key)
        return
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    with _lock:
        conn().execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,julianday('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value))


def kv_get(key: str, default: Any = None) -> Any:
    with _lock:
        row = conn().execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    v = row[0]
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.startswith(("{", "[", '"')) or s in ("null", "true", "false") \
            or (s and s[0].isdigit()):
        try:
            return json.loads(v)
        except Exception:
            pass
    return v


def kv_delete(key: str) -> None:
    with _lock:
        conn().execute("DELETE FROM kv WHERE key=?", (key,))


def kv_all() -> dict:
    with _lock:
        rows = conn().execute("SELECT key,value FROM kv").fetchall()
    out: dict = {}
    for k, v in rows:
        out[k] = kv_get(k)
    return out


# ---------------------------------------------------------------------------
# Chats + Turns
# ---------------------------------------------------------------------------

def upsert_chat(chat_id: str, open_id: str, model: Optional[str],
                workspace: Optional[str]) -> None:
    if not chat_id:
        return
    with _lock:
        conn().execute(
            "INSERT INTO chats(chat_id,open_id,model,workspace,"
            "created_at,last_used_at) "
            "VALUES(?,?,?,?,julianday('now'),julianday('now')) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "last_used_at=julianday('now'), "
            "model=COALESCE(?, model), workspace=COALESCE(?, workspace)",
            (chat_id, open_id, model, workspace, model, workspace))


def set_chat_title(chat_id: str, title: str) -> None:
    with _lock:
        conn().execute("UPDATE chats SET title=? WHERE chat_id=?",
                       (title, chat_id))


def get_chat_title(chat_id: str) -> Optional[str]:
    if not chat_id:
        return None
    with _lock:
        row = conn().execute("SELECT title FROM chats WHERE chat_id=?",
                             (chat_id,)).fetchone()
    return row[0] if row else None


def list_chats(open_id: Optional[str] = None, limit: int = 10) -> list[dict]:
    where, args = "", []
    if open_id:
        where = "WHERE open_id=?"
        args.append(open_id)
    with _lock:
        rows = conn().execute(
            f"SELECT chat_id, model, workspace, title, "
            f"strftime('%Y-%m-%d %H:%M', last_used_at) AS used, turn_count "
            f"FROM chats {where} ORDER BY last_used_at DESC LIMIT ?",
            (*args, limit)).fetchall()
    cols = ("chat_id", "model", "workspace", "title", "used", "turn_count")
    return [dict(zip(cols, r)) for r in rows]


def turn_start(chat_id: Optional[str], open_id: str, model: str,
               mode: Optional[str], prompt: str,
               card_id: Optional[str] = None) -> int:
    with _lock:
        cur = conn().execute(
            "INSERT INTO turns(chat_id, open_id, started_at, model, mode, "
            "prompt, card_id) VALUES(?,?,?,?,?,?,?)",
            (chat_id, open_id, time.time(), model, mode, prompt, card_id))
        return int(cur.lastrowid or 0)


def turn_end(turn_id: int, *, chat_id: Optional[str], result: Optional[str],
             usage: Optional[dict], error: Optional[str] = None) -> None:
    if not turn_id:
        return
    in_tok = (usage or {}).get("inputTokens", 0) or 0
    out_tok = (usage or {}).get("outputTokens", 0) or 0
    cache_tok = (usage or {}).get("cacheReadTokens", 0) or 0
    with _lock:
        conn().execute(
            "UPDATE turns SET finished_at=?, chat_id=COALESCE(?, chat_id), "
            "result=?, in_tokens=?, out_tokens=?, cache_read_tokens=?, "
            "error=? WHERE turn_id=?",
            (time.time(), chat_id, result, in_tok, out_tok, cache_tok,
             error, turn_id))
        if chat_id:
            conn().execute("UPDATE chats SET turn_count=turn_count+1, "
                           "last_used_at=julianday('now') WHERE chat_id=?",
                           (chat_id,))


def add_includes(turn_id: int, includes: list[dict]) -> None:
    if not turn_id or not includes:
        return
    with _lock:
        conn().executemany(
            "INSERT INTO includes(turn_id,path,size,truncated) VALUES(?,?,?,?)",
            [(turn_id, it.get("path", ""), it.get("size", 0),
              int(bool(it.get("truncated")))) for it in includes])


def recent_turns(open_id: Optional[str] = None, limit: int = 10) -> list[dict]:
    where, args = "", []
    if open_id:
        where = "WHERE open_id=?"
        args.append(open_id)
    with _lock:
        rows = conn().execute(
            f"SELECT turn_id, chat_id, model, "
            f"strftime('%m-%d %H:%M', started_at, 'unixepoch','localtime') "
            f"  AS started, "
            f"COALESCE(finished_at - started_at, 0) AS dur_s, "
            f"in_tokens, out_tokens, "
            f"COALESCE(substr(prompt,1,60),''), error "
            f"FROM turns {where} ORDER BY started_at DESC LIMIT ?",
            (*args, limit)).fetchall()
    cols = ("turn_id", "chat_id", "model", "started", "dur_s",
            "in_tokens", "out_tokens", "prompt", "error")
    return [dict(zip(cols, r)) for r in rows]


def cost_summary(open_id: Optional[str] = None,
                 since_unix: Optional[float] = None) -> dict:
    where_parts, args = [], []
    if open_id:
        where_parts.append("open_id=?")
        args.append(open_id)
    if since_unix is not None:
        where_parts.append("started_at>=?")
        args.append(since_unix)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with _lock:
        row = conn().execute(
            f"SELECT COUNT(*), COALESCE(SUM(in_tokens),0), "
            f"COALESCE(SUM(out_tokens),0), COALESCE(SUM(cache_read_tokens),0) "
            f"FROM turns {where}",
            args).fetchone()
    return {
        "turn_count": int(row[0] or 0),
        "in_tokens": int(row[1] or 0),
        "out_tokens": int(row[2] or 0),
        "cache_read_tokens": int(row[3] or 0),
    }


# ---------------------------------------------------------------------------
# Migration from legacy state.json (one-time)
# ---------------------------------------------------------------------------

def _migrate_from_legacy_json() -> None:
    """If ~/larksor-tc/state.json exists and DB is empty, copy keys over
    and rename the json file so we don't re-migrate."""
    if not LEGACY_STATE_FILE.exists():
        return
    # If db already has any kv rows, skip migration to avoid clobbering.
    with _lock:
        existing = conn().execute("SELECT COUNT(*) FROM kv").fetchone()[0]
    if existing > 0:
        return
    try:
        data = json.loads(LEGACY_STATE_FILE.read_text())
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        kv_set(k, v)
    try:
        LEGACY_STATE_FILE.rename(LEGACY_STATE_FILE.with_suffix(".json.bak"))
    except Exception:
        pass
