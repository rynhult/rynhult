"""Focused permission delivery, stale requests and ingestion regressions."""
import concurrent.futures
import json
import os
import socket

import pytest
from helpers import wait_for
from test_ingest_robustness import external, line, state

from acc.broker import ops

SID = "abcdef0123"
TOKEN = "a" * 32


def test_permission_reply_is_one_shot(env):
    folder = env["broker"].run_dir / SID
    folder.mkdir()
    # /proc/self/fd keeps long test paths within sockaddr_un limits.
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    path = f"/proc/self/fd/{fd}/permission-{TOKEN}.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        listener.listen(1)
        def receive():
            connection, _ = listener.accept()
            with connection:
                answer = connection.recv(16)
                listener.close()
                os.unlink(path)
                connection.sendall(b"answered")
                return answer
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            received = pool.submit(receive)
            replies = list(pool.map(lambda _: ops.permission_reply(env["broker"], SID, TOKEN, "allow"), range(2)))
            assert received.result(timeout=5) == b"allow"
        assert sum(r["answered"] for r in replies) == 1
        assert ops.permission_reply(env["broker"], SID, TOKEN, "deny") == {"answered": False}
    os.close(fd)


def test_permission_reply_rejects_paths(env):
    with pytest.raises(ops.OpError):
        ops.permission_reply(env["broker"], SID, "../../other", "allow")


async def test_permission_details_survive_notification(api, project, env):
    session, folder, sid = await external(api, project, env, "permission details")
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", tool_name="Bash",
        tool_input={"command": "git status"}) + line(sid, "Notification",
        notification_type="permission_prompt", message="Claude needs your permission"))
    await wait_for(lambda: _waiting(api, session["id"]))
    items = (await api.get("/api/attention")).json()
    item = next(i for i in items if i["session_id"] == session["id"])
    assert item["detail"] == "Bash: git status"
    # External sessions cannot receive dashboard input, even with a guessed token.
    response = await api.post(f"/api/attention/{item['id']}/permission", json={"token": TOKEN, "decision": "allow"})
    assert response.status_code == 403


async def _waiting(api, sid):
    return await state(api, sid) == "waiting_input"


async def test_copilot_advisory_hooks_do_not_wait(api, project, env):
    session, folder, sid = await external(api, project, env, "copilot advisory")
    records = [json.loads(line(sid, event)) for event in ("SessionStart", "PermissionRequest", "PreToolUse")]
    for record in records:
        record["provider"] = "copilot"
    (folder / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    await wait_for(lambda: _active(api, session["id"]))
    assert not any(i["session_id"] == session["id"] and i["kind"] == "needs_input"
                   for i in (await api.get("/api/attention")).json())


async def _active(api, sid):
    return await state(api, sid) == "active"


async def test_browser_answer_audit_stale_and_hidden_item(api, project, env, monkeypatch):
    from helpers import pause_supervisor, write_db
    from test_auth import cookie_client, do_setup

    from acc.server import attention
    from acc.server.models import AttentionItem, AuditLog, Session

    await do_setup(api, env)
    session, folder, sid = await external(api, project, env, "browser permission")
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", acc_permission_id=TOKEN,
        tool_name="Bash", tool_input={"command": "git status"}))
    await wait_for(lambda: _waiting(api, session["id"]))
    await pause_supervisor(api)
    item = next(i for i in (await api.get("/api/attention")).json() if i["session_id"] == session["id"])
    assert item["permission_id"] == TOKEN
    with write_db(api.app.state.db) as db:
        target = db.get(Session, session["id"])
        target.management = "acc"
        target.tmux_name = "acc-test"
        db.commit()
    calls = []
    async def reply(method, **params):
        calls.append((method, params))
        return {"answered": len(calls) == 1}
    monkeypatch.setattr(api.app.state.supervisor.broker, "call", reply)
    body = {"token": TOKEN, "decision": "deny"}
    assert (await api.post(f"/api/attention/{item['id']}/permission", json=body)).status_code == 403
    client = cookie_client(api)
    headers = {"Origin": "http://acc", "X-ACC-CSRF": client.cookies.get("acc_csrf")}
    from sqlalchemy import select

    from acc.server.models import Event
    with write_db(api.app.state.db) as db:
        event = db.scalar(select(Event).where(Event.session_id == session["id"], Event.kind == "PermissionRequest"))
        saved_payload = dict(event.payload)
        event.payload = {**event.payload, "acc_complete_input": None}
        db.commit()
    assert (await client.post(f"/api/attention/{item['id']}/permission",
        json={"token": TOKEN, "decision": "allow"}, headers=headers)).status_code == 409
    assert calls == []
    with write_db(api.app.state.db) as db:
        db.get(Event, event.id).payload = saved_payload
        db.commit()
    response = await client.post(f"/api/attention/{item['id']}/permission", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert (await client.post(f"/api/attention/{item['id']}/permission", json=body, headers=headers)).status_code == 409
    with write_db(api.app.state.db) as db:
        db.get(AttentionItem, item["id"]).resolved_at = attention.now()
        db.commit()
    assert (await client.post(f"/api/attention/{item['id']}/permission", json=body, headers=headers)).status_code == 409
    with write_db(api.app.state.db) as db:
        db.get(AttentionItem, item["id"]).owner = "someone-else"
        db.commit()
    assert (await client.post(f"/api/attention/{item['id']}/permission", json=body, headers=headers)).status_code == 404
    assert len(calls) == 2
    with api.app.state.db() as db:
        from sqlalchemy import select
        records = list(db.scalars(select(AuditLog).where(AuditLog.action == "permission.result")))
        assert {r.actor for r in records} == {"casey"}
        assert {r.detail["outcome"] for r in records} == {"answered", "already_answered"}
        import hashlib
        attempts = list(db.scalars(select(AuditLog).where(AuditLog.action == 'permission.answer')))
        assert {r.actor for r in attempts} == {'casey'}
        assert {r.detail['input_sha256'] for r in attempts} == {
            hashlib.sha256(saved_payload['acc_complete_input'].encode('utf-8')).hexdigest()}


async def test_command_is_vault_redacted_before_truncation(api, project, env):
    from helpers import write_db
    from test_vault import CANARY, add

    from acc.server.models import Session

    key = await add(api, "Permission secret", "PERMISSION_SECRET")
    session, folder, sid = await external(api, project, env, "redacted permission")
    with write_db(api.app.state.db) as db:
        db.get(Session, session["id"]).vault_keys = [key["id"]]
        db.commit()
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", tool_name="Bash",
        tool_input={"command": "echo " + CANARY + "x" * 1000}))
    await wait_for(lambda: _waiting(api, session["id"]))
    items = (await api.get("/api/attention")).json()
    item = next(i for i in items if i["session_id"] == session["id"])
    assert CANARY not in json.dumps(items)
    assert "«key: Permission secret»" in item["detail"]
    assert len(item["detail"]) <= 500


@pytest.mark.parametrize("tool_input", [
    {"command": "echo hello", "timeout": 3000},
    {"file_path": "/tmp/example", "content": "complete Write content"},
    {"file_path": "/tmp/example", "old_string": "before", "new_string": "after"},
    {"url": "https://example.com", "prompt": "complete fetch prompt"},
    {"description": "task", "prompt": "complete task prompt"},
    {"nested": {"arguments": [1, "complete MCP argument"]}},
    {"content": "x" * 9000},
    {"content": "y" * 3000},
])
async def test_complete_permission_input(api, project, env, tool_input):
    session, folder, sid = await external(api, project, env, "complete input")
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", acc_permission_id=TOKEN,
        tool_name="Tool", tool_input=tool_input))
    await wait_for(lambda: _waiting(api, session["id"]))
    item = next(i for i in (await api.get("/api/attention")).json() if i["session_id"] == session["id"])
    complete = len(json.dumps(tool_input).encode()) <= 8192
    assert item["permission_can_approve"] == complete
    if complete:
        assert json.loads(item["permission_input"]) == tool_input
    else:
        assert item["permission_input"] is None
        assert "8 kB" in item["permission_reason"]


async def test_permission_expiry_and_container(api, project, env):
    from helpers import write_db

    from acc.server.api.inbox import permission_info
    from acc.server.models import Session

    session, folder, sid = await external(api, project, env, "expiry")
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", acc_permission_id=TOKEN,
        tool_name="Bash", tool_input={"command": "true"}))
    await wait_for(lambda: _waiting(api, session["id"]))
    with write_db(api.app.state.db) as db:
        assert permission_info(db, session["id"])["permission_id"] == TOKEN
        db.get(Session, session["id"]).container = True
        db.commit()
        assert permission_info(db, session["id"]) == {}
        db.get(Session, session["id"]).container = False
        db.commit()
    with (folder / "events.jsonl").open("a") as stream:
        stream.write(line(sid, "PermissionExpired", acc_permission_id=TOKEN))
    async def expired():
        with api.app.state.db() as db:
            return permission_info(db, session["id"]) == {}
    await wait_for(expired)


def test_hook_attachment_fallbacks(monkeypatch, tmp_path):
    """Read embedded Python as source; never execute a bin/acc-* helper."""
    import ast
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    source = Path("bin/acc-hook").read_text().split("import json, os, signal", 1)[1].split("\n'", 1)[0]
    tree = ast.parse("import json, os, signal" + source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "remote_allowed")
    namespace = {"os": os, "subprocess": subprocess, "time": __import__("time")}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "embedded-hook", "exec"), namespace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMUX", "/tmp/synthetic,1,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    answers = {"#{@acc-terminal-snapshot}": "", "#{session_attached}": "0", "#{client_name}": "", "#{@acc-inbox-permissions}": "", "#{session_id}": "$1"}
    def run(args, **kw):
        assert args[:3] == ["/usr/bin/tmux", "-S", "/tmp/synthetic"]
        if args[3] == "display-message":
            assert args[4:8] == ["-p", "-t", "%2", args[-1]]
        return SimpleNamespace(stdout=answers[args[-1]])
    monkeypatch.setattr(subprocess, "run", run)
    assert namespace["remote_allowed"]()
    answers["#{@acc-terminal-snapshot}"] = str(__import__("time").time())
    assert not namespace["remote_allowed"]()
    answers["#{@acc-terminal-snapshot}"] = ""
    # The same check runs each accept tick and again before a decision.
    answers["#{session_attached}"] = "1"
    assert not namespace["remote_allowed"]()
    answers["#{session_attached}"] = "0"
    answers["#{client_name}"] = "browser observer or writer"
    assert not namespace["remote_allowed"]()
    answers["#{client_name}"] = ""
    answers["#{@acc-inbox-permissions}"] = "off"
    assert not namespace["remote_allowed"]()
    answers["#{@acc-inbox-permissions}"] = ""
    (tmp_path / "permission-transport").write_text("terminal")
    assert not namespace["remote_allowed"]()
    (tmp_path / "permission-transport").unlink()
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("tmux", .2)
    monkeypatch.setattr(subprocess, "run", failed)
    assert not namespace["remote_allowed"]()
    monkeypatch.delenv("TMUX")
    assert not namespace["remote_allowed"]()


def test_permission_toggle(env, monkeypatch):
    from types import SimpleNamespace

    enabled = True
    def tmux(config, *args, **kwargs):
        nonlocal enabled
        if args[0] == "set-option":
            enabled = args[-1] == "on"
        return SimpleNamespace(stdout="on" if enabled else "off")
    monkeypatch.setattr(ops, "tmux", tmux)
    assert ops.inbox_permissions(env["broker"], "acc-test") == {"enabled": True}
    assert ops.inbox_permissions(env["broker"], "acc-test", False) == {"enabled": False}
    assert ops.inbox_permissions(env["broker"], "acc-test", True) == {"enabled": True}


@pytest.mark.parametrize("attached_initially, completed", [(True, False), (False, False), (False, True)])
def test_hook_yields_on_attachment(tmp_path, monkeypatch, capsys, attached_initially, completed):
    import ast
    import io
    import signal
    import sys
    from pathlib import Path

    source = Path("bin/acc-hook").read_text().split("import json, os, signal", 1)[1].split("\n'", 1)[0]
    tree = ast.parse("import json, os, signal" + source)
    # Keep the production wait loop intact, supplying a synthetic attachment transition.
    tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name == "remote_allowed")]
    calls = []
    def allowed():
        calls.append(True)
        return completed or (not attached_initially and len(calls) == 1)
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, value):
            pass
        def recv(self, count):
            return b"allow"
        def sendall(self, value):
            assert value == b"answered"
    class Listener:
        def bind(self, name):
            (tmp_path / name).touch()
        def listen(self, count):
            pass
        def settimeout(self, value):
            assert value == .5
        def accept(self):
            if completed:
                return Connection(), None
            pytest.fail("Attachment must cancel before waiting for a reply")
        def close(self):
            pass
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACC_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("ACC_SESSION_ID", SID)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "PermissionRequest"})))
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    original_mask = os.umask(0o022)
    os.umask(original_mask)
    monkeypatch.setattr(os, "umask", lambda value: original_mask)
    monkeypatch.setattr(socket, "socket", lambda *args: Listener())
    try:
        exec(compile(tree, "embedded-hook", "exec"), {"remote_allowed": allowed})
    except SystemExit:
        pass
    output = capsys.readouterr().out
    assert bool(output) == completed
    if completed:
        assert json.loads(output)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert records[0]["acc_session"] == SID
    assert records[0]["event"] == "PermissionRequest"
    assert len(records) == (1 if attached_initially or completed else 2)
    if not attached_initially and not completed:
        assert records[-1]["hook"]["hook_event_name"] == "PermissionExpired"
    assert not list(tmp_path.glob("*.sock"))


async def test_toggle_writes_audit(api, project, env, monkeypatch):
    from helpers import pause_supervisor, write_db
    from test_auth import cookie_client, do_setup

    from acc.server.models import Session

    await do_setup(api, env)
    session, _, _ = await external(api, project, env, "toggle audit")
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        db.get(Session, session["id"]).management = "acc"
        db.commit()
    async def reply(method, **params):
        assert method == "inbox_permissions"
        return {"enabled": params["enabled"] if params["enabled"] is not None else True}
    monkeypatch.setattr(api.app.state.supervisor.broker, "call", reply)
    client = cookie_client(api)
    headers = {"Origin": "http://acc", "X-ACC-CSRF": client.cookies.get("acc_csrf")}
    path = f"/api/sessions/{session['id']}/inbox-permissions"
    assert (await client.get(path)).status_code == 200
    assert (await client.put(path, json={"enabled": False}, headers=headers)).status_code == 200
    rows = (await api.get("/api/audit", params={"action": "session.inbox_permissions"})).json()
    assert len(rows) == 1
    assert rows[0]["target"] == session["id"]
    assert rows[0]["detail"] == {"enabled": False}


async def test_enrolled_permission_answer_refuses_before_broker(api, project, env, monkeypatch):
    from helpers import pause_supervisor, write_db
    from test_auth import cookie_client, do_setup

    from acc.server.models import RelaySession

    await do_setup(api, env)
    session, folder, sid = await external(api, project, env, "relay permission fence")
    (folder / "events.jsonl").write_text(line(sid, "PermissionRequest", acc_permission_id=TOKEN,
        tool_name="Bash", tool_input={"command": "git status"}))
    await wait_for(lambda: _waiting(api, session["id"]))
    await pause_supervisor(api)
    item = next(i for i in (await api.get("/api/attention")).json() if i["session_id"] == session["id"])
    with write_db(api.app.state.db) as db:
        db.add(RelaySession(session_id=session['id'], enrollment='fixture-enrollment', binding_id='fixture-binding'))
        db.commit()
    async def never_reply(*_args, **_kwargs):
        pytest.fail('Enrolled permission answers must go through the hub')
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', never_reply)
    client = cookie_client(api)
    headers = {"Origin": "http://acc", "X-ACC-CSRF": client.cookies.get("acc_csrf")}
    for choice in ('allow', 'deny'):
        response = await client.post(f"/api/attention/{item['id']}/permission",
                                     json={'token': TOKEN, 'decision': choice}, headers=headers)
        assert response.status_code == 409, response.text
        assert 'qualified hub input' in response.json()['detail']


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_card_uses_bounded_hook_facts(api, project, env, provider, monkeypatch):
    from helpers import write_db

    from acc.server.models import Session

    session, folder, sid = await external(api, project, env, 'bounded card')
    with write_db(api.app.state.db) as db:
        db.get(Session, session['id']).provider = provider
        db.commit()
    record = json.loads(line(sid, 'PermissionRequest', cwd='/tmp/<example>', tool_name='Bash',
        tool_input={'command': '<script>literal</script>'}, acc_card={'command': 'forged'}, acc_complete_input='{"command":"forged"}'))
    record['provider'] = provider
    (folder / 'events.jsonl').write_text(json.dumps(record) + '\n')
    await wait_for(lambda: _waiting(api, session['id']))
    item = next(i for i in (await api.get('/api/attention')).json() if i['session_id'] == session['id'])
    assert item['approval_card'] == {'command': '<script>literal</script>', 'cwd': '/tmp/<example>',
                                     'diff': None, 'tool_name': 'Bash', 'file_path': None, 'label': 'No specific hint'}
    from acc.server.api.inbox import permission_info
    from acc.server.models import Event
    with write_db(api.app.state.db) as db:
        db.add(Event(session_id=session['id'], source='codex_hook' if provider == 'claude' else 'claude_hook',
            kind='PermissionRequest', payload={'acc_card': {'command': 'forged'}, 'acc_permission_id': TOKEN}))
        db.commit()
        assert permission_info(db, session['id'])['approval_card'] == item['approval_card']
    from acc.server import approval_cards
    with monkeypatch.context() as patch:
        patch.setattr(approval_cards, 'classify', lambda *a: pytest.fail('read path classified input'))
        patch.setattr(approval_cards, 'card', lambda *a: pytest.fail('read path rebuilt card'))
        listed = next(i for i in (await api.get('/api/attention')).json() if i['session_id'] == session['id'])
        assert listed['approval_card'] == item['approval_card']
    with (folder / 'events.jsonl').open('a') as stream:
        record['hook']['tool_input'] = {'command': 'é' * 5000}
        stream.write(json.dumps(record) + '\n')
    async def bounded():
        rows = (await api.get('/api/attention')).json()
        return next(i for i in rows if i['session_id'] == session['id'])['approval_card']['command'] is None
    await wait_for(bounded)


@pytest.mark.parametrize('provider', ['cursor', 'copilot', 'antigravity', 'opencode'])
async def test_other_providers_discard_forged_card_snapshot(api, project, env, provider):
    from helpers import write_db
    from sqlalchemy import select

    from acc.server import attention
    from acc.server.api.inbox import permission_info
    from acc.server.models import Event, Session

    session, folder, sid = await external(api, project, env, 'forged card ' + provider)
    with write_db(api.app.state.db) as db:
        db.get(Session, session['id']).provider = provider
        db.commit()
    record = json.loads(line(sid, 'PermissionRequest', tool_name='Bash', tool_input={'command': 'real command'},
        acc_card={'command': 5, 'label': {}}, acc_complete_input='{"command":"git status"}'))
    record['provider'] = provider
    (folder / 'events.jsonl').write_text(json.dumps(record) + '\n')

    async def ingested():
        with api.app.state.db() as db:
            return db.scalar(select(Event.id).where(Event.session_id == session['id'],
                                                    Event.kind == 'PermissionRequest')) is not None
    await wait_for(ingested)
    with write_db(api.app.state.db) as db:
        event = db.scalar(select(Event).where(Event.session_id == session['id'], Event.kind == 'PermissionRequest'))
        assert 'acc_card' not in event.payload
        assert 'acc_complete_input' not in event.payload
        assert permission_info(db, session['id']) == {}
        # Providers with advisory permission hooks may need a screen-detected item.
        attention.raise_item(db, key='round2-forged:' + session['id'], kind='needs_input', severity='warn',
                         source=f'{provider}_hook', title='Review actual prompt', session_id=session['id'])
        db.commit()
    rows = (await api.get('/api/attention')).json()
    owned = [row for row in rows if row['session_id'] == session['id']]
    assert owned
    assert all('approval_card' not in row and 'permission_input' not in row for row in owned)


async def test_native_terminal_notice_never_inherits_a_stale_remote_approval(api,project,env,monkeypatch):
    from helpers import pause_supervisor, write_db
    from test_auth import cookie_client, do_setup

    from acc.server.models import AttentionItem

    await do_setup(api,env)
    session,folder,sid=await external(api,project,env,'native terminal handoff')
    (folder/'events.jsonl').write_text(line(sid,'PermissionRequest',acc_permission_id=TOKEN,
        acc_complete_input='stale input',tool_name='Bash',tool_input={'command':'fixture'}))
    await wait_for(lambda:_waiting(api,session['id']));await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        item=db.query(AttentionItem).filter_by(session_id=session['id'],kind='needs_input').one()
        item.source='claude_terminal';item.detail='Claude is asking in the terminal; answer it there';item.verified=False
        item_id=item.id;db.commit()
    calls=[]
    async def refused(*args,**kwargs):calls.append(args);raise AssertionError('Native modal must not receive remote input')
    monkeypatch.setattr(api.app.state.supervisor.broker,'call',refused)
    item=next(item for item in (await api.get('/api/attention')).json() if item['id']==item_id)
    assert 'permission_id' not in item and 'permission_can_approve' not in item
    assert item['detail']=='Claude is asking in the terminal; answer it there'
    client=cookie_client(api);headers={'Origin':'http://acc','X-ACC-CSRF':client.cookies.get('acc_csrf')}
    reply=await client.post(f'/api/attention/{item_id}/permission',json={'token':TOKEN,'decision':'allow'},headers=headers)
    assert reply.status_code==409 and 'answer it there' in reply.text and not calls
