import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def policy():
    path = REPO / "src/acc/tooling.py"
    assert path.exists(), "the shared tool validator/executor is not implemented"
    spec = importlib.util.spec_from_file_location("tool_policy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("manager,name,version", [
    ("apt", "ripgrep", "14.1.0-1"), ("npm", "@example/tool", "1.2.3"),
    ("pnpm", "prettier", None), ("yarn", "prettier", "3.6.2"),
    ("pip", "my_tool", "1.2.3.post1"), ("pipx", "black", None), ("uv", "ruff", "0.14.0"),
    ("cargo", "ripgrep", "14.1.0"), ("go", "golang.org/x/tools/cmd/goimports", "v0.37.0"),
    ("gem", "rubocop", "1.2.3"), ("brew", "jq", None), ("snap", "jq", "42"),
    ("flatpak", "org.example.Tool", "a" * 64),
])
def test_registry_coordinates(manager, name, version):
    assert policy().validate(manager, name, version) == (manager, name, version)


@pytest.mark.parametrize("manager,name,version", [
    ("sh", "jq", None), ("dpkg", "jq", None), ("npm", "https://example.com/a", None),
    ("npm", "git+https://example.com/a", None), ("npm", "../tool", None), ("apt", "-oFoo", None),
    ("pipx", "black;touch bad", None), ("pip", "/tmp/a.whl", None), ("go", "./cmd/tool", None),
    ("go", "example.com/../tool", None), ("brew", "owner/tap/jq", None),
    ("apt", "jq", "1;id"), ("npm", "jq", "^1.0"), ("pip", "jq", "--user"),
    ("cargo", "jq", "https://example.com/a"), ("snap", "jq", "latest"),
    ("apt", "jq", ""), ("npm", "@x/../../bad", None),
])
def test_refuses_non_coordinates(manager, name, version):
    with pytest.raises(ValueError):
        policy().validate(manager, name, version)


@pytest.mark.parametrize('command', ['acc-request', 'rynhult-request'])
def test_request_cli_emits_validated_tool_event(tmp_path, command):
    script = REPO / 'bin/acc-request'
    if command.startswith('rynhult'):
        alias = tmp_path / command
        alias.symlink_to(script)
        script = alias
    env = {**os.environ, "ACC_RUN_DIR": str(tmp_path), "ACC_SESSION_ID": "demo"}
    result = subprocess.run([str(script), "tool", "--manager", "npm",
                             "--name", "@example/tool", "--version", "1.2.3", "--reason", "Format output"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    request = json.loads((tmp_path / "events.jsonl").read_text())["hook"]["request"]
    assert len(request.pop("request_id")) == 32
    assert "Queued, not confirmed" in result.stdout
    assert "waits in the Rynhult Inbox" not in result.stdout
    assert request == {"type": "tool", "manager": "npm", "name": "@example/tool", "version": "1.2.3",
                       "reason": "Format output", "action": "install"}
    for bad in ("--registry=https://example.com", "https://example.com/a", "../tool", "jq; id"):
        result = subprocess.run([str(script), "tool", "--manager", "npm", "--name", bad,
                                 "--reason", "test"], env=env, capture_output=True, text=True)
        assert result.returncode != 0
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1


def test_executor_pins_observed_version_and_refuses_wrong_scope(tmp_path):
    module = policy()
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        out = '{"dependencies":{"prettier":{"version":"3.6.2"}}}' if "ls" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    result = module.execute("npm", "prettier", "3.6.2", "install", home=tmp_path, system=False, run=run)
    assert result["version"] == "3.6.2" and result["ok"]
    assert commands[0] == ["npm", "install", "--global", "--prefix", str(tmp_path / ".local"),
                           "--no-audit", "--no-fund", "prettier@3.6.2"]
    with pytest.raises(ValueError):
        module.execute("npm", "prettier", None, "install", home=tmp_path, system=True, run=run)
    with pytest.raises(ValueError):
        module.execute("apt", "jq", None, "install", home=tmp_path, system=False, run=run)
    with pytest.raises(ValueError, match="version"):
        module.execute("npm", "prettier", "3.0.0", "install", home=tmp_path, system=False, run=run)


@pytest.mark.skipif(os.geteuid() != 0, reason="the helper loads only a root-owned tool policy")
def test_helper_tool_action_validates_every_field(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    (root / "requests").mkdir(parents=True)
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(root))
    loader = importlib.machinery.SourceFileLoader("tool_helper_test", str(REPO / "deploy/packages/acc-packages"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "install ok installed 14.1.0-1" if argv[0] == "dpkg-query" else "", "")

    monkeypatch.setattr(module, "run", run)
    # Model the installed helper's protected policy ownership without requiring root.
    trusted_policy = REPO / "src/acc/tooling.py"
    real_stat = Path.stat

    def installed_policy_stat(path, **kwargs):
        info = real_stat(path, **kwargs)
        if path in (trusted_policy, trusted_policy.parent):
            fields = list(info)
            fields[0] &= ~0o022  # installed policy is not group/world writable
            fields[4] = 0  # st_uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "stat", installed_policy_stat)
    body = {"id": "tool-000000000001", "action": "tool", "manager": "apt", "name": "ripgrep",
            "version": "14.1.0-1", "operation": "install"}
    path = root / "requests" / f"{body['id']}.json"
    path.write_text(json.dumps(body))
    result = module.process(module.load_state(), path)
    assert result["ok"] and result["version"] == "14.1.0-1", result
    assert commands[0] == ["apt-get", "install", "-y", "--no-install-recommends", "ripgrep=14.1.0-1"]
    commands.clear()
    for bad in ({"manager": "npm"}, {"name": "https://example.com/a"}, {"version": ";id"},
                {"command": "id"}, {"operation": "exec"}, {"url": "https://example.com"}):
        path.write_text(json.dumps({**body, **bad}))
        assert not module.process(module.load_state(), path)["ok"]
    assert commands == []


@pytest.mark.parametrize("manager,name,version,metadata", [
    ("snap", "jq", "42", "Name Version Rev Tracking Publisher Notes\njq 1.7 42 latest/stable example -"),
    ("flatpak", "org.example.Tool", "a" * 64, "a" * 64),
    ("pnpm", "prettier", "3.6.2", '[{"dependencies":{"prettier":{"version":"3.6.2"}}}]'),
    ("yarn", "prettier", "3.6.2", '{"type":"tree","data":{"trees":[{"name":"prettier@3.6.2"}]}}'),
    ("pip", "black", "25.1.0", '[{"name":"black","version":"25.1.0"}]'),
    ("pipx", "black", "25.1.0", '{"venvs":{"black":{"metadata":{"main_package":{"package_version":"25.1.0"}}}}}'),
    ("uv", "ruff", "0.14.0", "ruff v0.14.0\n- ruff"),
    ("cargo", "ripgrep", "14.1.0", "ripgrep v14.1.0:\n    rg"),
    ("go", "golang.org/x/tools/cmd/goimports", "v0.37.0", "\tmod golang.org/x/tools v0.37.0 h1:example"),
    ("gem", "rubocop", "1.2.3", "rubocop (1.2.3)"),
    ("brew", "jq", "1.7", '{"formulae":[{"installed":[{"version":"1.7"}]}]}'),
])
def test_each_manager_observes_its_real_metadata_shape(tmp_path, manager, name, version, metadata):
    module = policy()
    if manager == "go":
        (tmp_path / "go/bin").mkdir(parents=True)
        (tmp_path / "go/bin/goimports").touch()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        # The metadata is returned only by the manager's read command, never the install's output.
        reading = any(x in argv for x in ("list", "info", "--list", "--versions")) or argv[:3] == ["go", "version", "-m"]
        return subprocess.CompletedProcess(argv, 0, metadata if reading else "", "")

    result = module.execute(manager, name, version, "install", home=tmp_path, system=manager in module.SYSTEM, run=run)
    assert result["version"] == version
    assert not any(x in call for call in calls for x in ("sh", "bash", "sudo", "su"))


def test_pip_upgrade_and_snap_refresh_are_real_changes(tmp_path):
    module = policy()
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[{"name":"black","version":"25.1.0"}]' if "list" in argv else "", "")

    module.execute("pip", "black", None, "install", home=tmp_path, system=False, run=run)
    assert "--upgrade" in commands[0]
    commands.clear()

    def snap(argv, **kwargs):
        commands.append(argv)
        out = "Name Version Rev Tracking Publisher Notes\njq 1.7 42 latest/stable example -" if "list" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    module.execute("snap", "jq", "42", "install", home=tmp_path, system=True, run=snap)
    assert ["snap", "refresh", "jq", "--revision", "42"] in commands


@pytest.mark.parametrize("metadata", ['null', '{"dependencies":null}', '{"dependencies":{"prettier":null}}',
                                    '{"dependencies":{"prettier":{"version":42}}}'])
def test_malformed_version_metadata_fails_explicitly(tmp_path, metadata):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, metadata if "ls" in argv else "", "")

    with pytest.raises(ValueError, match="metadata"):
        policy().execute("npm", "prettier", None, "install", home=tmp_path, system=False, run=run)


def test_existing_flatpak_can_upgrade_and_uv_checks_for_updates(tmp_path):
    module = policy()
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        out = "a" * 64 if "info" in argv else "ruff v0.14.0" if argv == ["uv", "tool", "list"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    module.execute("flatpak", "org.example.Tool", None, "install", home=tmp_path, system=True, run=run)
    assert "--or-update" in commands[0]
    commands.clear()
    module.execute("uv", "ruff", None, "install", home=tmp_path, system=False, run=run)
    assert "--upgrade" in commands[0]


@pytest.mark.parametrize("manager,name,absent", [
    ("apt", "ripgrep", "dpkg-query: no packages found matching ripgrep"),
    ("snap", "jq", "error: no matching snaps installed"),
    ("flatpak", "org.example.Tool", "error: org.example.Tool is not installed"),
])
def test_removal_distinguishes_absence_from_an_unreadable_package_database(tmp_path, manager, name, absent):
    module = policy()

    def run(argv, **kwargs):
        reading = argv[0] == "dpkg-query" or argv[1] in ("list", "info")
        return subprocess.CompletedProcess(argv, 1 if reading else 0, "", "package database unavailable" if reading else "")

    with pytest.raises(ValueError, match="package database unavailable"):
        module.execute(manager, name, None, "remove", home=tmp_path, system=True, run=run)

    def missing(argv, **kwargs):
        reading = argv[0] == "dpkg-query" or argv[1] in ("list", "info")
        return subprocess.CompletedProcess(argv, 1 if reading else 0, "", absent if reading else "")

    assert module.execute(manager, name, None, "remove", home=tmp_path, system=True, run=missing)["ok"]


@pytest.mark.parametrize("manager,metadata", [
    ("npm", '{"dependencies":{"prettier":{"version":"3.6.2"}}}'),
    ("pnpm", '[{"dependencies":{"prettier":{"version":"3.6.2"}}}]'),
    ("yarn", '{"type":"tree","data":{"trees":[{"name":"prettier@3.6.2"}]}}'),
])
def test_approved_account_packages_can_run_their_install_lifecycle(tmp_path, manager, metadata):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, metadata if any(a in argv for a in ("list", "ls")) else "", "")

    policy().execute(manager, "prettier", None, "install", home=tmp_path, system=False, run=run)
    argv, env = calls[0]
    assert "--ignore-scripts" not in argv
    assert env["HOME"] == str(tmp_path)


@pytest.fixture(autouse=True)
def no_live_session_reply_lookup(monkeypatch):
    # Standalone CLI tests must never inherit the caller's real session identity.
    monkeypatch.delenv('ACC_SESSION_ID', raising=False)
    monkeypatch.delenv('RYNHULT_SESSION_ID', raising=False)


@pytest.mark.parametrize('status,code,text', [('accepted', 0, 'Sent for approval:'), ('refused', 1, 'Refused: Reserve port 8102')])
def test_request_cli_waits_for_server_and_status_reads_same_reply(tmp_path, status, code, text):
    import time

    from acc.request_replies import publish
    protected = tmp_path / 'protected'
    protected.mkdir(mode=0o711)
    spool = tmp_path / 'spool'
    spool.mkdir()
    # Disposable installed-helper fixture: simulate the service principal's
    # ownership in a single-UID test. Production has no anchor/UID env override.
    helper = tmp_path / 'acc-request'
    source = (REPO / 'bin/acc-request').read_text().replace(
        "REPLY_ROOT = Path('/var/lib/acc-request-replies')", f"REPLY_ROOT = Path({str(protected)!r})").replace(
        '    owners = {0}', '    owners = {0, os.getuid()}')
    helper.write_text(source)
    helper.chmod(0o755)
    (tmp_path / 'acc_env.py').write_text((REPO / 'src/acc/env.py').read_text())
    session = 'abcdef123456'
    environ = {**os.environ, 'ACC_RUN_DIR': str(spool), 'ACC_SESSION_ID': session}
    process = subprocess.Popen([str(helper), 'publish', '--host', 'demo.example.com', '--port', '8102'],
        env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 4
        event = None
        while time.monotonic() < deadline:
            try:
                event = json.loads((spool / 'events.jsonl').read_text())
                break
            except (FileNotFoundError, ValueError):
                time.sleep(.01)
        assert event is not None
        rid = event['hook']['request']['request_id']
        publish(protected, session, {rid: {'status':status, 'approval_id':'pub-1', 'reason':'Reserve port 8102'}})
        # Replacing the legacy attachment with a well-formed opposite reply
        # cannot override the server's accepted/refused acknowledgement.
        (spool / 'request-replies.json').write_text(json.dumps({rid: {
            'status': 'refused' if status == 'accepted' else 'accepted', 'approval_id':'forged-by-agent', 'reason':'forged'}}))
        out, err = process.communicate(timeout=8)
        assert process.returncode == code, err
        assert text in out and 'forged' not in out
        lookup = subprocess.run([str(helper), 'status', rid], env=environ, capture_output=True, text=True)
        assert lookup.returncode == code and text in lookup.stdout and 'forged' not in lookup.stdout
        assert len((spool / 'events.jsonl').read_text().splitlines()) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize('attachment', ['symlink', 'fifo', 'large', 'malformed'])
def test_request_status_does_not_follow_or_block_on_untrusted_attachments(tmp_path, attachment):
    path = tmp_path / 'request-replies.json'
    if attachment == 'symlink':
        other = tmp_path / 'other'
        other.write_text(json.dumps({'a'*32: {'status':'accepted', 'approval_id':'forged'}}))
        path.symlink_to(other)
    elif attachment == 'fifo':
        os.mkfifo(path)
    else:
        path.write_text('x' * 65537 if attachment == 'large' else '{')
    result = subprocess.run([str(REPO / 'bin/acc-request'), 'status', 'a'*32],
        env={**os.environ, 'ACC_RUN_DIR':str(tmp_path)}, capture_output=True, text=True, timeout=2)
    assert result.returncode == 0
    assert 'Queued, not confirmed' in result.stdout and 'Sent for approval' not in result.stdout


@pytest.mark.parametrize('status', ['accepted', 'refused'])
def test_request_cli_ignores_agent_forged_regular_reply(tmp_path, status, capsys, monkeypatch):
    import runpy
    helper = runpy.run_path(str(REPO / 'bin/acc-request'), run_name='request_test')
    monkeypatch.setenv('ACC_SESSION_ID', 'abcdef123456')
    monkeypatch.setenv('RYNHULT_SESSION_ID', 'abcdef123456')
    helper['reply'].__globals__['REPLY_ROOT'] = tmp_path / 'missing-protected-store'
    rid = 'c' * 32
    (tmp_path / 'request-replies.json').write_text(json.dumps({rid: {
        'status': status, 'approval_id': 'forged-by-agent', 'reason': 'Forged refusal'}}))
    assert helper['reply'](tmp_path, rid) is None
    assert capsys.readouterr().out == ''
