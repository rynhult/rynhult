"""The broker's read_spool (multi-host): what a session can put in its run folder never makes it read anything but
its own events file, and never blocks."""
from __future__ import annotations

import base64
import os

import pytest

from acc.broker import ops

ACC_ID = "abcdef0123"


def read(env, offset: int = 0) -> bytes:
    return base64.b64decode(ops.read_spool(env["broker"], ACC_ID, offset)["data"])


@pytest.fixture
def run(env):
    folder = env["broker"].run_dir / ACC_ID
    folder.mkdir()
    return folder


def test_reads_from_an_offset(env, run):
    (run / "events.jsonl").write_bytes(b"one\ntwo\n")
    assert read(env) == b"one\ntwo\n"
    assert read(env, 4) == b"two\n"
    assert read(env, 100) == b""


def test_a_missing_spool_is_empty(env, run):
    assert read(env) == b""
    assert base64.b64decode(ops.read_spool(env["broker"], "nosuchsess1", 0)["data"]) == b""


def test_reads_are_bounded(env, run):
    (run / "events.jsonl").write_bytes(b"x" * (ops.SPOOL_READ + 10))
    assert len(read(env)) == ops.SPOOL_READ
    assert read(env, ops.SPOOL_READ) == b"x" * 10


def test_a_linked_events_file_is_refused(env, run, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("someone else's file\n")
    (run / "events.jsonl").symlink_to(secret)
    with pytest.raises(ops.OpError, match="isn't a plain file"):
        read(env)


def test_a_linked_session_folder_is_refused(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "events.jsonl").write_text("not this session's\n")
    (env["broker"].run_dir / ACC_ID).symlink_to(elsewhere)
    with pytest.raises(ops.OpError, match="isn't a plain file"):
        read(env)


def test_a_fifo_is_refused_without_blocking(env, run):
    os.mkfifo(run / "events.jsonl")
    with pytest.raises(ops.OpError, match="isn't a plain file"):
        read(env)


def test_a_folder_is_refused(env, run):
    (run / "events.jsonl").mkdir()
    with pytest.raises(ops.OpError, match="isn't a plain file"):
        read(env)


def test_a_file_owned_by_someone_else_is_refused(env, run, monkeypatch):
    (run / "events.jsonl").write_text("x\n")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ops.OpError, match="belongs to another account"):
        read(env)


@pytest.mark.parametrize("acc_id, offset", [("../x", 0), (ACC_ID, -1), (ACC_ID, "0"), (ACC_ID, True)])
def test_bad_arguments(env, acc_id, offset):
    with pytest.raises(ops.OpError, match="invalid session ID or read position"):
        ops.read_spool(env["broker"], acc_id, offset)
