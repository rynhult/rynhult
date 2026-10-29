"""deploy/root-session/acc-root-session: config parsing and conversation following (dry run, no tmux or claude)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/root-session/acc-root-session"
OLD = "aaaaaaaa-1111-2222-3333-444444444444"
NEW = "bbbbbbbb-1111-2222-3333-444444444444"
ACC_ID = "b79c92170367"

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="the script creates root-owned folders")


@pytest.fixture
def box(tmp_path):
    claude = tmp_path / "claude"
    (claude / "projects/-root").mkdir(parents=True)
    (claude / f"projects/-root/{OLD}.jsonl").write_text("{}\n")
    work = tmp_path / "work"
    work.mkdir()
    conf = tmp_path / "conf"
    conf.write_text(f"# comment\nSESSION_ID={OLD}\nACC_ID={ACC_ID}\nWORKDIR={work}\nPERMISSION_MODE=auto\n"
                    "TMUX_SOCKET=acc-root\nTMUX_SESSION=acc-root\n")
    env = {**os.environ, "ACC_ROOT_SESSION_CONF": str(conf), "ACC_ROOT_SESSION_STATE": str(tmp_path / "state/id"),
           "ACC_ROOT_RUN_DIR": str(tmp_path / "run-root"), "ACC_ROOT_CLAUDE_DIR": str(claude),
           "ACC_ROOT_SESSION_DRY_RUN": "1"}

    def run():
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    def spool(*session_ids):
        run_dir = tmp_path / "run-root" / ACC_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "events.jsonl").open("a") as handle:
            for sid in session_ids:
                handle.write(json.dumps({"hook": {"hook_event_name": "SessionStart", "source": "compact", "session_id": sid}}) + "\n")
    return {"tmp": tmp_path, "conf": conf, "claude": claude, "work": work, "run": run, "spool": spool}


def test_configured_conversation_and_root_owned_state(box):
    result = box["run"]()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == OLD
    state = box["tmp"] / "state/id"
    assert state.read_text().strip() == OLD and (state.stat().st_mode & 0o777) == 0o600
    assert (box["tmp"] / "run-root" / ACC_ID).is_dir()


def test_follows_a_continued_conversation_but_not_glob_patterns_or_missing_transcripts(box):
    (box["claude"] / f"projects/-root/{NEW}.jsonl").write_text("{}\n")
    pattern = "[ab]" + NEW[1:]  # 36 characters that would glob-match a real transcript
    box["spool"](NEW, pattern, "cccccccc-1111-2222-3333-444444444444", "x'; touch pwned; '")
    result = box["run"]()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == NEW
    assert box["conf"].read_text().count(OLD) == 1  # the config file is never rewritten
    assert not (box["tmp"] / "pwned").exists()
    assert box["run"]().stdout.strip() == NEW  # next boot: the root-only state file remembers it


@pytest.mark.parametrize("line", [
    "SESSION_ID=$(touch {tmp}/pwned)", "SESSION_ID=x; touch {tmp}/pwned", "EXTRA=1", "not a setting",
    "PERMISSION_MODE=auto --dangerously-skip-permissions", "TMUX_SESSION=acc root",
])
def test_bad_config_is_refused_without_running_anything(box, line):
    box["conf"].write_text(box["conf"].read_text() + line.format(tmp=box["tmp"]) + "\n")
    result = box["run"]()
    assert result.returncode == 1 and "acc-root-session:" in result.stderr
    assert not (box["tmp"] / "pwned").exists()


def test_workdir_must_be_owned_by_root(box):
    os.chown(box["work"], 65534, 65534)
    result = box["run"]()
    assert result.returncode == 1 and "WORKDIR" in result.stderr


def test_symlinked_run_folder_is_refused(box):
    (box["tmp"] / "run-root").mkdir()
    (box["tmp"] / "elsewhere").mkdir()
    (box["tmp"] / "run-root" / ACC_ID).symlink_to(box["tmp"] / "elsewhere")
    result = box["run"]()
    assert result.returncode == 1 and "symlink" in result.stderr


def test_conversation_folder_follows_a_moved_workdir(box):
    (box["claude"] / "projects/-root/memory").mkdir()
    (box["claude"] / "projects/-root/memory/MEMORY.md").write_text("notes")
    assert box["run"]().returncode == 0
    moved = box["claude"] / "projects" / str(box["work"]).replace("/", "-").replace("_", "-")
    assert (moved / f"{OLD}.jsonl").exists() and (moved / "memory/MEMORY.md").read_text() == "notes"
    assert (box["claude"] / f"projects/-root/{OLD}.jsonl").exists()  # the original stays


def test_newer_transcript_replaces_a_stale_copy(box):
    assert box["run"]().returncode == 0
    moved = box["claude"] / "projects" / str(box["work"]).replace("/", "-").replace("_", "-")
    source = box["claude"] / f"projects/-root/{OLD}.jsonl"
    source.write_text("{}\n{\"later\": 1}\n")  # the session kept going
    newer = (moved / f"{OLD}.jsonl").stat().st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(newer, newer))
    assert box["run"]().returncode == 0
    assert "later" in (moved / f"{OLD}.jsonl").read_text()
