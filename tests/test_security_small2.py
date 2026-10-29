"""Regression proofs for the owner-machine feature trust boundaries, using synthetic data."""
import subprocess

import pytest
from test_auth import live  # noqa: F401
from test_member_views import member  # noqa: F401
from test_multihost import connect_standin
from test_node_packages import agent_adds, broker_env, node_dry_run, stubs  # noqa: F401

from acc.broker import ops
from acc.server.agent_setup import guide
from acc.server.broker_client import BrokerError, Brokers


@pytest.mark.parametrize("account", ["root", "alex; echo unsafe", "$(echo unsafe)", "-u root"])
def test_guide_never_offers_an_unsafe_or_root_account(account):
    steps = guide("claude", "linux", account, "example-node")
    assert steps["shell_command"] is None


@pytest.mark.parametrize("unsafe", ["writable", "symlink"])
def test_node_refresh_refuses_untrusted_settings(stubs, tmp_path, unsafe):  # noqa: F811
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent")
    config = env_dir / "alex-agent.env"
    if unsafe == "writable":
        config.chmod(0o666)
    else:
        target = tmp_path / "outside.env"
        config.rename(target)
        config.symlink_to(target)
    result = node_dry_run(stubs, env_dir, "--update")
    assert agent_adds(result.stdout) == []
    assert result.returncode == 1


def test_node_refresh_refuses_root_account(stubs, tmp_path):  # noqa: F811
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "root")
    result = node_dry_run(stubs, env_dir, "--update")
    assert agent_adds(result.stdout) == []
    assert result.returncode == 1


@pytest.mark.parametrize("roots", ["relative", "/safe\\nLD_PRELOAD=/example.so", '"/quoted"', "/a/../b", "/a::/b"])
def test_node_refresh_refuses_unsafe_roots(stubs, tmp_path, roots):  # noqa: F811
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent", roots=roots)
    result = node_dry_run(stubs, env_dir, "--update")
    assert agent_adds(result.stdout) == []
    assert result.returncode == 1


@pytest.mark.parametrize("method", ["ping", "make_workspace"])
async def test_new_project_api_withholds_remote_credentials(api, env, live, tmp_path, monkeypatch, method):  # noqa: F811
    _, task, _ = await connect_standin(api, env, live, tmp_path)
    real = Brokers.call

    async def hostile(self, requested, account=None, host=None, **params):
        if host == "pc" and requested == method:
            raise BrokerError("Bearer sample-private-token https://alex:sample-password@example.com token=sample-key")
        return await real(self, requested, account=account, host=host, **params)
    monkeypatch.setattr(Brokers, "call", hostile)
    try:
        result = await api.post("/api/projects/new", json={"name": "Safe project", "host": "pc"})
        assert result.status_code in (409, 502, 503)
        assert "sample-" not in result.text
        assert not (env["workspaces"] / "safe-project").exists()
    finally:
        task.cancel()


@pytest.mark.parametrize("operation", ["clone_url", "clone_repo"])
def test_clone_failure_withholds_native_credentials(env, monkeypatch, operation):
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "Bearer sample-private-token https://alex:sample-password@example.com")
    monkeypatch.setattr(ops.subprocess, "run", failed)
    with pytest.raises(ops.OpError) as failure:
        getattr(ops, operation)(env["broker"], "https://example.com/team/repo" if operation == "clone_url" else "team/repo",
                                str(env["workspaces"] / "safe-project"))
    assert "clone failed" in str(failure.value)
    assert "sample-" not in str(failure.value)


async def test_new_project_refuses_newline_slug(api, env):
    result = await api.post("/api/projects/new", json={"name": "Safe", "slug": "safe\n"})
    assert result.status_code == 422
    assert not (env["workspaces"] / "safe\n").exists()


def test_broker_refuses_newline_folder(env):
    with pytest.raises(ops.OpError, match="Use a folder name"):
        ops.make_workspace(env["broker"], str(env["workspaces"]), "safe\n", "Safe")
    assert not (env["workspaces"] / "safe\n").exists()


async def test_new_project_refuses_a_nodes_root_account(api, env, live, tmp_path):  # noqa: F811
    _, task, _ = await connect_standin(api, env, live, tmp_path)
    api.app.state.ctx.broker.hub.links["pc"].accounts = ["root"]
    try:
        result = await api.post("/api/projects/new", json={"name": "Unsafe", "host": "pc"})
        assert result.status_code == 422
        assert not (env["workspaces"] / "unsafe").exists()
    finally:
        task.cancel()


@pytest.mark.parametrize("slug", ["../outside", "/absolute", "--option", "a/b", "a;word", "a\x00b"])
async def test_new_project_refuses_path_and_argument_names(api, env, slug):
    result = await api.post("/api/projects/new", json={"name": "Safe", "slug": slug})
    assert result.status_code == 422
    assert not (env["workspaces"] / "safe").exists()


@pytest.mark.parametrize("url", ["ext::example", "file:///etc", "--upload-pack=example", "https://alex:sample-password@example.com/repo", "https://example.com/repo?token=sample-key"])
async def test_remote_clone_refuses_argument_and_credential_urls(api, env, live, tmp_path, url):  # noqa: F811
    _, task, _ = await connect_standin(api, env, live, tmp_path)
    try:
        response = await api.post("/api/projects/new", json={"name": "Safe", "source": "clone", "host": "pc", "url": url})
        assert response.status_code == 422
        assert "sample-" not in response.text
        assert not (env["workspaces"] / "safe").exists()
    finally:
        task.cancel()


def test_windows_version_probe_disables_command_processor_autorun(tmp_path):
    from acc.node.identity import windows_version
    cmd = tmp_path / "cmd.exe"
    cmd.touch()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Microsoft Windows [Version 10.0.22631.1]", "")
    assert windows_version(run=run, cmd=cmd) == "Windows 11 (build 22631)"
    assert calls == [[str(cmd), "/d", "/c", "ver"]]


async def test_remote_project_authority_is_the_members_own_account(api, env, live, tmp_path, member, monkeypatch):  # noqa: F811
    from test_users import csrf
    node, task, _ = await connect_standin(api, env, live, tmp_path)
    api.app.state.ctx.broker.hub.links["pc"].accounts = ["dana-agent", "other-agent"]
    workspace_calls = []
    real = Brokers.call

    async def record_workspace(self, requested, account=None, host=None, **params):
        if requested == "make_workspace":
            workspace_calls.append((account, host))
        return await real(self, requested, account=account, host=host, **params)

    monkeypatch.setattr(Brokers, "call", record_workspace)
    try:
        denied = await member.post("/api/projects/new", headers=csrf(member), json={"name": "Forbidden", "host": "pc", "account": "other-agent"})
        assert denied.status_code == 422
        assert "other-agent" not in denied.text
        assert workspace_calls == []
        # Model the supervisor's legitimate per-account sign-in-health poll.
        await api.app.state.ctx.broker.call("auth_health", account="other-agent", host="pc")
        made = await member.post("/api/projects/new", headers=csrf(member), json={"name": "Own project", "host": "pc"})
        assert made.status_code == 201, made.text
        assert made.json()["account"] == "dana-agent" and made.json()["host"] == "pc"
        assert workspace_calls == [("dana-agent", "pc")]
        assert "dana-agent" in node.opened
    finally:
        task.cancel()


@pytest.mark.parametrize("kind", ["parent-link", "directory-writable", "fifo", "duplicate-role", "duplicate-roots", "large", "hardlink"])
def test_broker_reader_rejects_unsafe_file_custody(tmp_path, kind):
    import os
    from pathlib import Path
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent")
    config = env_dir / "alex-agent.env"
    if kind == "parent-link":
        alias = tmp_path / "linked-broker"
        alias.symlink_to(env_dir, target_is_directory=True)
        env_dir = alias
    elif kind == "directory-writable":
        env_dir.chmod(0o777)
    elif kind == "fifo":
        config.unlink()
        os.mkfifo(config)
    elif kind == "duplicate-role":
        with config.open("a") as stream:
            stream.write("ACC_BROKER_ROLE=services\n")
    elif kind == "duplicate-roots":
        with config.open("a") as stream:
            stream.write("ACC_ALLOWED_ROOTS=/other\n")
    elif kind == "large":
        with config.open("a") as stream:
            stream.write("#" * 65537)
    else:
        os.link(config, tmp_path / "alias.env")
    reader = Path(__file__).resolve().parents[1] / "deploy/lib/node-accounts.py"
    result = subprocess.run(["/usr/bin/python3", "-I", str(reader), str(env_dir), "--owner", str(os.getuid()), "--dry-run"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and not result.stdout.strip()


@pytest.mark.parametrize("uid", [0, 999, 65534])
def test_broker_reader_rejects_system_uid_aliases(tmp_path, monkeypatch, uid):
    import os
    import pwd
    import runpy
    from pathlib import Path
    from types import SimpleNamespace
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent")
    reader = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/lib/node-accounts.py"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid))
    with pytest.raises(ValueError, match="system account"):
        reader["read_accounts"](env_dir, os.getuid(), True)


async def test_packages_api_does_not_offer_a_node_supplied_shell_command(api, env, live, tmp_path):  # noqa: F811
    _, task, _ = await connect_standin(api, env, live, tmp_path)
    link = api.app.state.ctx.broker.hub.links["pc"]
    link.accounts = ["alex; echo unsafe"]
    link.packages = {**link.packages, "helper": False}
    try:
        response = await api.get("/api/packages", params={"machine": "pc"})
        assert response.status_code == 200
        assert response.json()["packages"]["claude"]["guide"]["shell_command"] is None
    finally:
        task.cancel()


def test_account_refresh_never_provisions_through_a_workspace_link(tmp_path):
    import os
    import pwd
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "deploy/broker/acc-agent-add").read_text()
    fragment = source[source.index('  first_root=${roots%%:*}'):source.index('  # Codex reads')]
    victim = tmp_path / "unrelated"
    victim.mkdir()
    parent = tmp_path / "workspace-parent"
    parent.symlink_to(victim, target_is_directory=True)
    # Exercise privileged fallback regardless of the suite uid. Real runuser to the
    # current account would run mkdir as root in root gates, bypassing the intended
    # agent-authority boundary. Model an agent unable to provision this folder.
    script = "runuser() { return 1; }\n" + fragment
    result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True,
                            env={**os.environ, "roots": str(parent / "new-folder"), "account": pwd.getpwuid(os.getuid()).pw_name}, timeout=5)
    assert result.returncode != 0
    assert not (victim / "new-folder").exists(), result.stderr


def test_agent_created_workspace_root_stays_private(tmp_path):
    import os
    import pwd
    import stat
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "deploy/broker/acc-agent-add").read_text()
    fragment = source[source.index('  first_root=${roots%%:*}'):source.index('  # Codex reads')]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Stand-in for root's runuser: the same account, so the agent-authority branch runs unprivileged.
    (bin_dir / "runuser").write_text('#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\numask 022\nexec "$@"\n')
    (bin_dir / "runuser").chmod(0o755)
    root = tmp_path / "agent-home" / "workspaces"
    result = subprocess.run(["bash", "-eu", "-c", fragment], capture_output=True, text=True, timeout=5,
                            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "roots": str(root),
                                 "account": pwd.getpwuid(os.getuid()).pw_name})
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(root.stat().st_mode) == 0o750
