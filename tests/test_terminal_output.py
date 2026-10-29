"""Accessible text snapshots read tmux history rather than only a browser's current screen."""
from __future__ import annotations

import subprocess

import pytest
from helpers import control, wait_for
from test_supervisor import launch, state_is

from acc.broker import ops


async def test_terminal_output_reads_history_and_is_not_cached(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    folder = env["run_dir"] / session["id"]
    (folder / "fake.screen").write_text("\x1b[32mFirst history marker\x1b[0m\n" + "middle row\n" * 100 + "Last history marker\n")
    control(env, session["id"], "screen")

    async def captured():
        response = await api.get(f'/api/sessions/{session["id"]}/terminal-output')
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        return response.json() if "Last history marker" in response.json()["text"] else None

    result = await wait_for(captured)
    assert "First history marker" in result["text"] and "\x1b" not in result["text"]
    assert result["truncated"] is False
    # Reading neither acquires terminal ownership nor sends input.
    assert (await api.get(f'/api/sessions/{session["id"]}')).json()["observed_state"] == "idle"


def test_terminal_output_bounds_large_replies(env, monkeypatch):
    monkeypatch.setattr(ops, "tmux", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "old" + "z" * 200_000, ""))
    result = ops.terminal_output(env["broker"], "acc-demo-12345678")
    assert result == {"text": "z" * 200_000, "truncated": True}


def test_terminal_output_refuses_bad_targets_and_capture_failures(env, monkeypatch):
    with pytest.raises(ops.OpError, match="invalid session terminal name"):
        ops.terminal_output(env["broker"], "other-session")
    monkeypatch.setattr(ops, "tmux", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "no pane"))
    with pytest.raises(ops.OpError, match="couldn.t read the session.s terminal"):
        ops.terminal_output(env["broker"], "acc-demo-12345678")


# ---------------------------------------------------------------------- security review (2026-10-24)

CANARY = "sk-canary-7f3a9c1e5b2d4f60aa"


def fake_capture(api, monkeypatch, text, calls=None):
    """The broker's reply to terminal_output, without tmux: what a session (or an enrolled machine) sends back."""
    broker = api.app.state.supervisor.broker
    real = broker.call

    async def call(method, **params):
        if method != "terminal_output":
            return await real(method, **params)
        if calls is not None:
            calls.append(params)
        return {"text": text, "truncated": False}
    monkeypatch.setattr(broker, "call", call)


async def test_snapshot_redacts_the_sessions_vault_keys(api, project, monkeypatch):
    """Transcripts, diffs and exports hide the values of the keys a session was given; the text snapshot is the same
    kind of copyable record and must too (the live terminal stays a raw view of the process)."""
    from acc.server.models import Session as SessionRow
    key = (await api.post("/api/vault/keys", json={"name": "OpenAI (work)", "variable": "OPENAI_API_KEY",
                                                   "value": CANARY})).json()
    session = await launch(api, project)
    with api.app.state.db() as s:
        s.get(SessionRow, session["id"]).vault_keys = [key["id"]]
        s.commit()
    fake_capture(api, monkeypatch, f"$ env\nOPENAI_API_KEY={CANARY}\ncut {CANARY[:16]}\n")
    response = await api.get(f'/api/sessions/{session["id"]}/terminal-output')
    assert response.status_code == 200, response.text
    assert CANARY[:14] not in response.text
    assert "«key: OpenAI (work)»" in response.json()["text"]


async def test_snapshot_reads_are_audited(api, project, monkeypatch):
    """Opening the live terminal is audited (terminal.attach); reading its text must leave the same trail."""
    session = await launch(api, project)
    fake_capture(api, monkeypatch, "hello")
    assert (await api.get(f'/api/sessions/{session["id"]}/terminal-output')).status_code == 200
    entries = (await api.get("/api/audit", params={"action": "terminal.read"})).json()
    assert [e["target"] for e in entries] == [session["id"]]
    assert "hello" not in str(entries)  # the trail records the read, never the text


async def test_snapshot_reads_are_rate_limited(api, project, monkeypatch):
    """Each read makes the broker capture up to ~10,000 rows of up to 1,000 columns: bound how often one person can."""
    session = await launch(api, project)
    calls = []
    fake_capture(api, monkeypatch, "x", calls)
    codes = [(await api.get(f'/api/sessions/{session["id"]}/terminal-output')).status_code for _ in range(40)]
    assert codes[0] == 200 and codes[-1] == 429
    assert len(calls) == codes.count(200) < 40


async def test_server_bounds_the_reply_itself(api, project, monkeypatch):
    """An enrolled machine answers for its own broker: the server enforces the cap rather than trusting the reply."""
    session = await launch(api, project)
    fake_capture(api, monkeypatch, "old" + "z" * 300_000)
    result = (await api.get(f'/api/sessions/{session["id"]}/terminal-output')).json()
    assert result == {"text": "z" * 200_000, "truncated": True}

    fake_capture(api, monkeypatch, {"not": "text"})
    assert (await api.get(f'/api/sessions/{session["id"]}/terminal-output')).status_code == 502
