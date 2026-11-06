"""Fixture-only transcript-following feasibility check; never discovers account files.

Run from repository root: PYTHONPATH=src .venv/bin/python docs/design/chat-spike/replay.py
No CLI, provider calls, live data, credentials, persistent state or new dependencies.
This deliberately exercises the current snapshot parsers, not a production cursor.
"""
from __future__ import annotations

import json
import queue
import tempfile
import threading
from pathlib import Path

from acc.providers import transcripts


def record(provider: str, role: str, text: str) -> bytes:
    content = [{"type": "text", "text": text}]
    if provider == "claude":
        value = {"type": role, "message": {"content": content}}
    else:
        value = {"type": "response_item", "payload": {
            "type": "message", "role": role, "content": content}}
    return json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"


def replay(provider: str, directory: Path) -> list[str]:
    path = directory / f"{provider}.jsonl"
    path.touch(mode=0o600)
    prompt = record(provider, "user", "Review the example.lan diagram")
    reply = record(provider, "assistant", "Ready é 🌲")
    # End the first write inside a UTF-8 code point and unfinished JSON.
    split = reply.index("🌲".encode("utf-8")) + 2
    writes = [prompt, reply[:split], reply[split:], reply]
    expected = [1, 1, 2, 3]
    notices: queue.Queue[int | BaseException] = queue.Queue()
    acknowledged = threading.Event()
    cancelled = threading.Event()

    def produce() -> None:
        try:
            for index, data in enumerate(writes):
                if cancelled.is_set():
                    return
                with path.open("ab") as writer:
                    writer.write(data)
                notices.put(index)
                # Reader must inspect the incomplete append before it is completed.
                if not acknowledged.wait(30):
                    raise TimeoutError("fixture reader did not acknowledge append")
                acknowledged.clear()
        except BaseException as error:
            notices.put(error)

    writer = threading.Thread(target=produce, name=f"chat-spike-{provider}")
    writer.start()
    seen: list[dict] = []
    emitted: list[str] = []
    try:
        for _ in writes:
            notice = notices.get(timeout=30)
            if isinstance(notice, BaseException):
                raise notice
            current = getattr(transcripts, provider)(path)
            assert len(current) == expected[notice], (provider, notice, current)
            assert current[:len(seen)] == seen, "fixture history unexpectedly changed"
            for message in current[len(seen):]:
                emitted.append(message["text"])
                print(json.dumps({"provider": provider, **message}, ensure_ascii=False))
            seen = current
            acknowledged.set()
    finally:
        cancelled.set()
        acknowledged.set()
        writer.join(timeout=30)
        assert not writer.is_alive(), "fixture writer leaked"
    assert emitted == ["Review the example.lan diagram", "Ready é 🌲", "Ready é 🌲"]
    return emitted


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="chat-spike-") as scratch:
        for provider in ("claude", "codex"):
            replay(provider, Path(scratch))
    print("PASS: both fixture streams; partial writes withheld; repeated messages preserved")
