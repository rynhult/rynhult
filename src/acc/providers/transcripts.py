"""Each agent CLI's conversation file, read as plain messages for ACC's history search.

Only what was said is kept: the user's prompts and the agent's replies, not tool output, system prompts or the
context wrappers CLIs add around a prompt. Formats checked against real files (2026-10-21): Claude Code 2.x,
Codex 0.163, Copilot CLI 1.0.102, Cursor 2026.10.19, Antigravity 1.4.4, OpenCode 2.1.3. Unknown lines are skipped, never
fatal.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_MESSAGES = 5000
MAX_CHARS = 8000
MAX_BYTES = 64 << 20  # input read per conversation file; MAX_MESSAGES and MAX_CHARS only bound what is kept
MAX_LINE = 4 << 20  # a longer JSONL line (or Cursor blob) is skipped
MAX_STORE = 256 << 20  # a larger Cursor store.db (plus its -wal) isn't read
WRAPPED = re.compile(r"<(user_query|USER_REQUEST)>\s*(.*?)\s*</\1>", re.S)
CONTEXT_ONLY = re.compile(r"^\s*<(environment_context|user_info|system-reminder|command-|local-command|"
                          r"ADDITIONAL_METADATA|timestamp)", re.S)


def _text(content) -> str:
    """Text from a string, or from a list of content blocks (text / input_text / output_text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")
                         and isinstance(b.get("text"), str))
    return ""


def _prompt(text: str) -> str:
    """The user's own words from a prompt a CLI wrapped in context tags; "" when it's context only."""
    wrapped = WRAPPED.search(text)
    if wrapped:
        return wrapped.group(2)
    return "" if CONTEXT_ONLY.match(text) else text


def _add(out: list[dict], role: str, text: str, ts=None) -> None:
    text = (text or "").strip()
    if text and len(out) < MAX_MESSAGES:
        out.append({"role": role, "text": text[:MAX_CHARS], "ts": ts if isinstance(ts, str) else None})


def _open(path: Path) -> int | None:
    """A descriptor of the regular file at path, or None: the agent writes these files and could put a link or a
    FIFO there, so the leaf is never followed, the open never blocks and the type is checked on the descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _lines(path: Path, out: list[dict]):
    """JSON objects, one per line, until `out` holds MAX_MESSAGES or MAX_BYTES have been read. A line longer than
    MAX_LINE is skipped without being held in memory whole."""
    fd = _open(path)
    if fd is None:
        return
    with os.fdopen(fd, "rb") as handle:
        budget = MAX_BYTES
        while budget > 0 and len(out) < MAX_MESSAGES:
            line = handle.readline(min(MAX_LINE + 1, budget))
            if not line:
                return
            budget -= len(line)
            if len(line) > MAX_LINE:
                while not line.endswith(b"\n") and budget > 0:  # drain the rest of it in bounded pieces
                    line = handle.readline(min(1 << 20, budget))
                    if not line:
                        return
                    budget -= len(line)
                continue
            try:
                record = json.loads(line.decode(errors="replace"))
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def claude(path: Path) -> list[dict]:
    out: list[dict] = []
    for r in _lines(path, out):
        message = r.get("message") if isinstance(r.get("message"), dict) else {}
        if r.get("type") == "user" and not r.get("isMeta"):
            _add(out, "user", _prompt(_text(message.get("content"))), r.get("timestamp"))
        elif r.get("type") == "assistant":
            _add(out, "assistant", _text(message.get("content")), r.get("timestamp"))
    return out


def codex(path: Path) -> list[dict]:
    out: list[dict] = []
    for r in _lines(path, out):
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        if r.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
            text = _text(payload.get("content"))
            _add(out, payload["role"], _prompt(text) if payload["role"] == "user" else text, r.get("timestamp"))
    return out


def copilot(path: Path) -> list[dict]:
    out: list[dict] = []
    for r in _lines(path, out):
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        if r.get("type") == "user.message":
            _add(out, "user", _prompt(_text(data.get("content"))), r.get("timestamp"))
        elif r.get("type") == "assistant.message":
            _add(out, "assistant", _text(data.get("content")), r.get("timestamp"))
    return out


def antigravity(path: Path) -> list[dict]:
    out: list[dict] = []
    for r in _lines(path, out):
        if r.get("type") == "USER_INPUT":
            _add(out, "user", _prompt(_text(r.get("content"))), r.get("created_at"))
        elif r.get("type") == "PLANNER_RESPONSE":
            _add(out, "assistant", _text(r.get("content")), r.get("created_at"))
    return out


def cursor(path: Path) -> list[dict]:
    """store.db: JSON message blobs ({"role", "content"}) among protobuf ones; stored in conversation order.
    Rows are fetched one at a time; blobs over MAX_LINE are never loaded, and reading stops at MAX_MESSAGES or
    after MAX_BYTES."""
    out: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="acc-cursor-") as tmp:
        if not _copy_store(path, Path(tmp)):
            return out
        try:
            db = sqlite3.connect(Path(tmp) / "store.db")
        except sqlite3.Error:
            return out
        _read_cursor(db, out)
    return out


def _copy_store(path: Path, tmp: Path) -> bool:
    """Copies store.db and its -wal (committed changes not yet checkpointed) into a private folder through descriptors
    checked by _open: SQLite opens by name, so it would follow a link swapped in after a check and block on a FIFO."""
    budget = MAX_STORE
    for name in (path.name, path.name + "-wal"):
        fd = _open(path.with_name(name))
        if fd is None:
            if name == path.name:
                return False
            continue
        with os.fdopen(fd, "rb") as src, open(tmp / ("store.db" + name[len(path.name):]), "wb") as dst:
            while chunk := src.read(min(1 << 20, budget + 1)):
                dst.write(chunk)
                budget -= len(chunk)
                if budget < 0:  # too big, or growing while copied
                    return False
    return True


def _read_cursor(db: sqlite3.Connection, out: list[dict]) -> None:
    try:
        budget = MAX_BYTES
        rows = db.execute("SELECT CASE WHEN length(data) <= ? THEN data END FROM blobs ORDER BY rowid", (MAX_LINE,))
        for (data,) in rows:
            if not isinstance(data, bytes) or not data.startswith(b"{"):
                continue
            budget -= len(data)
            if budget < 0:
                break
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("role") in ("user", "assistant"):
                text = _text(message.get("content"))
                _add(out, message["role"], _prompt(text) if message["role"] == "user" else text)
                if len(out) >= MAX_MESSAGES:
                    break
    except sqlite3.Error:
        pass  # keep what was read
    finally:
        db.close()


def _opencode_db(path: Path) -> sqlite3.Connection | None:
    """OpenCode's database, read-only. It is the account's one database (every conversation and saved sign-in), so it is
    queried in place rather than copied; a link or FIFO the agent put there is refused before SQLite opens it."""
    for name in (path.name, path.name + "-wal"):
        try:
            info = os.lstat(path.with_name(name))
        except FileNotFoundError:
            if name == path.name:
                return None
            continue
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        db.execute("PRAGMA query_only = ON")
        return db
    except sqlite3.Error:
        return None


def _opencode_tables(db: sqlite3.Connection) -> tuple[str | None, str | None]:
    """The sessions table (session_v2 in 2.1; any session* table with an id column) and the messages table."""
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    columns = {name: {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')} for name in names
               if re.fullmatch(r"session[a-z0-9_]*", name)}
    sessions = next((n for n in sorted(columns, key=lambda n: (n != "session_v2", n)) if {"id", "directory"} <= columns[n]), None)
    messages = "session_message" if {"session_id", "type", "data"} <= columns.get("session_message", set()) else None
    return sessions, messages


def opencode_has(path: Path, native_id: str) -> bool:
    """Whether OpenCode's database holds this session (a resume must never start a new conversation)."""
    db = _opencode_db(path)
    if db is None:
        return False
    try:
        table = _opencode_tables(db)[0]
        return bool(table and db.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (native_id,)).fetchone())
    except sqlite3.Error:
        return False
    finally:
        db.close()


def opencode(path: Path, native_id: str) -> list[dict]:
    """session_message rows of one session: type user ({"text"}) and assistant ({"content": [{"type": "text"}, …]})."""
    out: list[dict] = []
    db = _opencode_db(path)
    if db is None:
        return out
    try:
        table = _opencode_tables(db)[1]
        if not table:
            return out
        budget = MAX_BYTES
        order = "seq" if "seq" in {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')} else "rowid"
        rows = db.execute(f'SELECT type, CASE WHEN length(data) <= ? THEN data END FROM "{table}" '
                          f'WHERE session_id = ? AND type IN (\'user\', \'assistant\') ORDER BY {order}', (MAX_LINE, native_id))
        for kind, data in rows:
            if not isinstance(data, str):
                continue
            budget -= len(data)
            if budget < 0:
                break
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            created = (message.get("time") or {}).get("created") if isinstance(message.get("time"), dict) else None
            ts = _ms_iso(created)
            if kind == "user":
                _add(out, "user", message.get("text") if isinstance(message.get("text"), str) else "", ts)
            else:
                _add(out, "assistant", _text(message.get("content")), ts)
            if len(out) >= MAX_MESSAGES:
                break
    except sqlite3.Error:
        pass  # keep what was read
    finally:
        db.close()
    return out


def _ms_iso(value) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (OverflowError, OSError, ValueError):
        return None
