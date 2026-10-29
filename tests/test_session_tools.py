"""Session page tools: open in editor, branch and pull request, feedback to the agent, export."""
from __future__ import annotations

import dataclasses
import subprocess
from datetime import datetime, timezone

import pytest
from helpers import control, wait_for
from isolated_settings import isolated_broker
from test_ownership import two  # noqa: F401 (fixture)
from test_supervisor import get, launch, state_is
from test_users import csrf

from acc.broker import ops
from acc.server.config import Settings


def test_editor_host_defaults_to_the_dashboards_https_host():
    assert Settings(allowed_origins=("http://127.0.0.1:8740", "https://acc.example.net")).editor_host == "acc.example.net"
    assert Settings(allowed_origins=("http://127.0.0.1:8740",)).editor_host == ""  # no https origin: no link
    assert Settings(allowed_origins=("https://acc.example.net",), ssh_host="box.lan").editor_host == "box.lan"


@pytest.fixture
def ssh_env(env):
    env["settings"] = dataclasses.replace(env["settings"], ssh_host="acc.example.net")
    return env


async def test_session_detail_says_where_to_open_it_in_an_editor(ssh_env, api, project):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    detail = await get(api, session["id"])
    assert detail["editor"] == {"ssh_host": "acc.example.net", "path": detail["cwd"]}


async def test_no_editor_link_without_a_host(api, project):
    session = await launch(api, project)
    assert (await get(api, session["id"]))["editor"] is None


def broker_config(tmp_path):
    return isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket=f"acc-test-{tmp_path.name[-8:]}",
                        allowed_roots=(tmp_path,), allowed_uids=frozenset(), providers={}, hook_path="x")


@pytest.mark.parametrize("name,text", [
    ("acc-x; rm", "hello"), ("other", "hello"), ("acc-ok", ""), ("acc-ok", "   "), ("acc-ok", "x" * 4001),
    ("acc-ok", "fine\x1b[2Jthen"), ("acc-ok", "submit\rearly"), ("acc-ok", "\x03"), ("acc-ok", "c1\x9bcontrol"),
])
def test_send_text_refuses_bad_names_and_control_characters(tmp_path, name, text):
    with pytest.raises(ops.OpError):
        ops.send_text(broker_config(tmp_path), name, text)


class FakePane:
    def __init__(self, accepts_after=1, marker=None):
        self.accepts_after = accepts_after
        self.marker = marker
        self.input = ""
        self.received = []
        self.enters = 0

    def tmux(self, config, *args, check=False):
        output = ""
        if args[0] == "send-keys":
            if "-l" in args:
                self.input = args[-1]
            else:
                assert args[-1] == "Enter"
                self.enters += 1
                if self.input and self.enters >= self.accepts_after:
                    self.received.append(self.input)
                    self.input = ""
        elif args[0] == "display-message":
            output = "2"
        else:
            assert args[0] == "capture-pane"
            output = "› " + ((self.marker or self.input) if self.input else "")
            if "-S" not in args:
                output = "Old transcript: [Pasted Content 1200 chars]\n" + output
        return subprocess.CompletedProcess(args, 0, output, "")


@pytest.mark.parametrize("provider", ["claude", "codex", "copilot", "cursor", "antigravity", "opencode"])
@pytest.mark.parametrize("marker", [
    None, "[Pasted Content 1200 chars]", "[Pasted text #1 +12 lines]", "[Paste #1]", "[Pasted ~12 lines]",
])
def test_send_text_submits_once_even_when_paste_needs_another_enter(tmp_path, monkeypatch, provider, marker):
    text = "x" * 1200 if marker else "Review:\n- a.py:1\t`x` — fix"
    fake = FakePane(accepts_after=2 if marker else 1, marker=marker)
    monkeypatch.setattr(ops, "tmux", fake.tmux)
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    line = text.replace("\n", " ").replace("\t", " ")
    assert ops.send_text(broker_config(tmp_path), "acc-ok", text, provider=provider) == {"sent": len(line)}
    assert fake.received == [line]
    assert fake.enters == (2 if marker else 1)


def test_send_text_bounds_retries_when_provider_never_accepts(tmp_path, monkeypatch):
    fake = FakePane(accepts_after=100, marker="[Pasted Content 1200 chars]")
    monkeypatch.setattr(ops, "tmux", fake.tmux)
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    with pytest.raises(ops.OpError, match="message typed but not submitted"):
        ops.send_text(broker_config(tmp_path), "acc-ok", "x" * 1200, provider="codex")
    assert fake.received == []
    assert 1 < fake.enters <= 3


def pane(env, tmux_name):
    return subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "capture-pane", "-p", "-t", f"={tmux_name}:"],
                          capture_output=True, text=True).stdout


async def test_message_reaches_the_agent_and_is_audited(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    text = "Review feedback on your changes:\n- app.py:2 `two` — say three"
    response = await api.post(f"/api/sessions/{session['id']}/message", json={"text": text})
    assert response.status_code == 200, response.text

    async def typed():
        return "say three" in pane(env, session["tmux_name"])
    await wait_for(typed)
    audit = (await api.get("/api/audit", params={"action": "session.message"})).json()
    assert audit[0]["target"] == session["id"] and audit[0]["detail"]["text"] == text
    assert (await api.post(f"/api/sessions/{session['id']}/message", json={"text": "x" * 4001})).status_code == 422
    assert (await api.post(f"/api/sessions/{session['id']}/message", json={"text": "\x1b[A"})).status_code == 422
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(state_is(api, session["id"], "stopped"))
    stopped = await api.post(f"/api/sessions/{session['id']}/message", json={"text": "still there?"})
    assert stopped.status_code == 409 and "Resume it first" in stopped.text


async def test_message_reports_unconfirmed_submission(api, project, monkeypatch):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    original = ops.tmux
    fake = FakePane(accepts_after=100)

    def tmux(config, *args, **kwargs):
        if args[0] in ("send-keys", "display-message", "capture-pane"):
            return fake.tmux(config, *args, **kwargs)
        return original(config, *args, **kwargs)

    monkeypatch.setattr(ops, "tmux", tmux)
    response = await api.post(f"/api/sessions/{session['id']}/message", json={"text": "not accepted"})
    assert response.status_code == 502, response.text
    assert "message typed but not submitted" in response.text


@pytest.mark.parametrize("accepted", [False, True])
async def test_message_confirmation_ignores_old_hooks_and_handles_fast_turns(api, project, env, monkeypatch, accepted):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    original = ops.tmux
    fake = FakePane()
    spool_path = env["run_dir"] / session["id"] / "events.jsonl"

    def tmux(config, *args, **kwargs):
        if args[0] in ("send-keys", "display-message", "capture-pane"):
            result = fake.tmux(config, *args, **kwargs)
            if accepted and args[-1] == "Enter":
                with spool_path.open("a") as spool:
                    spool.write('{"hook":{"hook_event_name":"UserPromptSubmit","prompt":"new message"}}\n'
                                '{"hook":{"hook_event_name":"Stop"}}\n')
            return result
        return original(config, *args, **kwargs)

    monkeypatch.setattr(ops, "tmux", tmux)
    with spool_path.open("a") as spool:
        spool.write('{"hook":{"hook_event_name":"UserPromptSubmit","prompt":"an older message"}}\n')
    response = await api.post(f"/api/sessions/{session['id']}/message", json={"text": "new message"})
    assert response.status_code == (200 if accepted else 409), response.text
    if accepted:
        assert response.json()["observed_state"] == "idle"
    else:
        assert "couldn't confirm it was sent" in response.text
    assert fake.received == ["new message"]
    assert fake.enters == 1


async def test_a_member_cannot_message_someone_elses_session(two):  # noqa: F811 (fixture)
    member, session = two["member"], two["session"]
    response = await member.post(f"/api/sessions/{session['id']}/message", json={"text": "do something else"},
                                 headers=csrf(member))
    assert response.status_code == 404
    audit = (await two["owner"].get("/api/audit", params={"action": "session.message"})).json()
    assert audit == []


async def test_export_is_a_markdown_file_of_the_whole_session(api, project, env):
    session = await launch(api, project, name="Fix: login")
    await wait_for(state_is(api, session["id"], "idle"))
    handoffs = env["workspaces"] / "demo" / "docs" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / f"2026-10-22-{session['id']}-login.md").write_text("# Login fixed\n\n```sh\nnpm test\n```\n")
    (handoffs / "2026-10-22-someoneelse-other.md").write_text("# Not this session's\n")
    control(env, session["id"], "say please migrate the billing webhooks")

    async def indexed():
        response = await api.get(f"/api/sessions/{session['id']}/export")
        return response if "billing webhooks" in response.text and "| State | idle |" in response.text else None
    response = await wait_for(indexed)
    assert response.headers["content-type"].startswith("text/markdown")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert response.headers["content-disposition"] == f'attachment; filename="Fix-login-{day}.md"'
    doc = response.text
    assert doc.startswith("# Fix: login\n") and "| Project | demo |" in doc and "| Agent | claude |" in doc
    assert "| State | idle |" in doc
    conversation = doc.split("## Conversation", 1)[1].split("## Handoffs", 1)[0]
    assert "### You" in conversation and "billing webhooks" in conversation
    handoff = doc.split("## Handoffs", 1)[1].split("## Launch command", 1)[0]
    assert f"### docs/handoffs/2026-10-22-{session['id']}-login.md" in handoff and "# Login fixed" in handoff
    assert "````markdown" in handoff  # longer than the fence inside, so the document stays intact
    assert "Not this session's" not in doc
    assert "fake-claude" in doc.split("## Launch command", 1)[1]


async def test_a_member_cannot_export_someone_elses_session(two):  # noqa: F811 (fixture)
    response = await two["member"].get(f"/api/sessions/{two['session']['id']}/export")
    assert response.status_code == 404 and "Content-Disposition" not in response.headers


def test_send_text_sees_a_paste_marker_wrapped_above_the_cursor(monkeypatch):
    """Codex 2026-10-23: a long message folded into "[Pasted Content …]" wrapped onto the next row, the cursor row
    alone looked clear, and the unsubmitted message was reported as sent."""
    from acc.broker import ops
    calls = []

    def fake_tmux(config, *args, check=False):
        calls.append(args)
        out = ""
        if args[0] == "display-message":
            out = "10\n"
        elif args[0] == "capture-pane" and "-S" in args:
            start = args[args.index("-S") + 1]
            out = "and reply with the table\n" if start == "10" else "› [Pasted Content 1024 chars]They take\nand reply with the table\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(ops, "tmux", fake_tmux)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    with pytest.raises(ops.OpError, match="not submitted"):
        ops.send_text(None, "acc-x-12345678", "x" * 1100, provider="codex")
    assert sum(1 for a in calls if a[:1] == ("send-keys",) and a[-1] == "Enter") == 3
