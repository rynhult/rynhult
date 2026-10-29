"""Exercise privileged machine setup with process-boundary doubles, never real accounts."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from test_node_packages import run, stub

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_world(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    helper = tmp_path / "wsl-node.sh"
    shutil.copy(REPO / "deploy/lib/acc-env.sh", tmp_path / "acc-env.sh")
    helper.write_text(
        (REPO / "deploy/node/windows/wsl-node.sh").read_text()
        .replace("/root/acc-src", str(tmp_path / "installed"))
        .replace("/etc/acc/broker", str(tmp_path / "brokers"))
    )
    return helper, tools, {"PATH": f"{tools}:{os.environ['PATH']}", "ACC_WSL_CONF": str(tmp_path / "wsl.conf")}


@pytest.mark.parametrize("metadata", ["file", "dangling-link"])
def test_owner_refuses_existing_broker_before_any_account_changes(setup_world, tmp_path, metadata):
    helper, tools, env = setup_world
    brokers = tmp_path / "brokers"
    brokers.mkdir()
    if metadata == "file":
        (brokers / "alex.env").write_text("ACC_BROKER_ROLE=agents\n")
    else:
        (brokers / "alex.env").symlink_to(brokers / "missing")
    stub(tools, "id", "exit 0")
    for tool in ("useradd", "usermod", "passwd", "chage"):
        stub(tools, tool, f'echo "{tool} $*" >> "{tmp_path}/accounts.log"')
    result = run(["sh", str(helper), "owner", "alex"], **env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "broker" in result.stderr
    assert not (tmp_path / "accounts.log").exists()
    assert not (tmp_path / "wsl.conf").exists()


@pytest.mark.parametrize("before, after", [("NP", "L"), ("P", "P"), ("L", "L")])
def test_existing_owner_never_keeps_an_empty_password(setup_world, tmp_path, before, after):
    helper, tools, env = setup_world
    state = tmp_path / "password-state"
    state.write_text(before)
    stub(tools, "id", "exit 0")
    stub(tools, "passwd", f'''
case "$*" in
  "-S alex") printf 'alex %s 2026-10-23 0 99999 7 -1\\n' "$(cat "{state}")" ;;
  "-l alex") printf L > "{state}" ;;
  *) exit 88 ;;
esac
''')
    stub(tools, "usermod", "exit 0")
    result = run(["sh", str(helper), "owner", "alex"], **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == after
    assert (tmp_path / "wsl.conf").read_text() == "\n[user]\ndefault=alex\n"


def test_owner_password_query_failure_prevents_promotion(setup_world, tmp_path):
    helper, tools, env = setup_world
    stub(tools, "id", "exit 0")
    stub(tools, "passwd", "exit 2")
    stub(tools, "usermod", f'touch "{tmp_path}/promoted"')
    result = run(["sh", str(helper), "owner", "alex"], **env)
    assert result.returncode == 1
    assert "password" in result.stderr
    assert not (tmp_path / "promoted").exists()
    assert not (tmp_path / "wsl.conf").exists()


def test_owner_can_keep_its_services_only_broker(setup_world, tmp_path):
    helper, tools, env = setup_world
    brokers = tmp_path / "brokers"
    brokers.mkdir()
    (brokers / "alex.env").write_text("ACC_BROKER_ROLE=services\n")
    stub(tools, "id", "exit 0")
    stub(tools, "passwd", 'echo "alex P 2026-10-23 0 99999 7 -1"')
    stub(tools, "usermod", f'touch "{tmp_path}/promoted"')
    result = run(["sh", str(helper), "owner", "alex"], **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "promoted").exists()


@pytest.fixture
def bundled_source(setup_world, tmp_path, monkeypatch):
    helper, tools, env = setup_world
    source = tmp_path / "release"
    (source / "deploy/node/windows").mkdir(parents=True)
    (source / "bin").mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (source / ".acc-build").write_text("bundled-build\n")
    (source / "deploy/node/install.sh").write_text(
        '#!/bin/bash\nprintf "bundled %s\\n" "$*" > "$INSTALL_LOG"\n'
    )
    shutil.copyfile(REPO / "deploy/node/windows/source.sh", source / "deploy/node/windows/source.sh")
    stub(tools, "curl", 'exit "$CURL_STATUS"')
    monkeypatch.chdir(source)
    return helper, source, {**env, "INSTALL_LOG": str(tmp_path / "install.log")}


@pytest.mark.parametrize("curl_status", [5, 6, 7, 28])
def test_network_failure_cannot_select_bundled_installer_without_consent(bundled_source, tmp_path, curl_status):
    helper, _, env = bundled_source
    result = run(
        ["sh", str(helper), "install-primary", "https://acc.example.lan", "system", "-", "--agent", "alex-agent"],
        **env, CURL_STATUS=str(curl_status),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "--allow-bundled-source" in result.stderr
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / "install.log").exists()


def test_explicit_offline_consent_is_visible_and_not_forwarded_to_installer(bundled_source, tmp_path):
    helper, _, env = bundled_source
    result = run(
        ["sh", str(helper), "install-primary", "https://acc.example.lan", "system", "-",
         "--allow-bundled-source", "--agent", "alex-agent"], **env, CURL_STATUS="7",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" in result.stderr and "older" in result.stderr
    assert "0.1.0" in result.stderr
    assert (tmp_path / "install.log").read_text() == "bundled --install-prereqs --agent alex-agent\n"


@pytest.mark.parametrize("curl_status", [22, 60, 63])
def test_offline_consent_does_not_bypass_download_security_failures(bundled_source, tmp_path, curl_status):
    helper, _, env = bundled_source
    result = run(
        ["sh", str(helper), "install-primary", "https://acc.example.lan", "system", "-",
         "--allow-bundled-source", "--agent", "alex-agent"], **env, CURL_STATUS=str(curl_status),
    )
    assert result.returncode == 1
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / "install.log").exists()


def test_successful_fetch_runs_only_downloaded_installer(bundled_source, tmp_path):
    helper, source, env = bundled_source
    stub(source / "deploy/node/windows", "source.sh", '''
mkdir -p "$1/deploy/node"
cat > "$1/deploy/node/install.sh" <<'INSTALL'
#!/bin/bash
printf "verified %s\\n" "$*" > "$INSTALL_LOG"
INSTALL
''')
    result = run(
        ["sh", str(helper), "install-primary", "https://acc.example.lan", "system", "-",
         "--allow-bundled-source", "--agent", "alex-agent"], **env, CURL_STATUS="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "install.log").read_text() == "verified --install-prereqs --agent alex-agent\n"


@pytest.mark.parametrize("policy, expected", [
    ("direct", 1), ("other-group", 1), ("denied", 77), ("denied-exit0", 77), ("error", 1), ("ambiguous-error", 1),
])
def test_agent_checks_effective_sudo_policy_before_provisioning(tmp_path, policy, expected):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "id", 'case "$1" in -nG) echo agents-team ;; -u) echo 1001 ;; *) exit 0 ;; esac')
    stub(tools, "getent", 'echo "alex-agent:x:1001:1001::${HOME}:/bin/bash"')
    stub(tools, "install", f'echo provisioning > "{tmp_path}/provisioned"; exit 77')
    responses = {
        "direct": ('User alex-agent may run the following commands on example:\\n    (ALL) ALL', 0),
        "other-group": ('User alex-agent may run the following commands on example:\\n    (root) /usr/bin/id', 0),
        "denied": ('User alex-agent is not allowed to run sudo on example.', 1),
        # Ubuntu 24.04's sudo reports a denial with exit 0 (the main host's deploy stopped on this, 2026-10-23).
        "denied-exit0": ('User alex-agent is not allowed to run sudo on example.', 0),
        "error": ('sudo: policy plugin failed', 2),
        "ambiguous-error": ('sudo: unable to initialize policy plugin', 1),
    }
    output, status = responses[policy]
    stub(tools, "sudo", f'''
[ "$*" = "-n -l -U alex-agent" ] || exit 88
printf '%b\\n' '{output}'
exit {status}
''')
    result = run(
        ["bash", str(REPO / "deploy/broker/acc-agent-add"), "alex-agent"],
        PATH=f"{tools}:{os.environ['PATH']}", ACC_AGENT_RUN_ROOT=str(tmp_path / "run"),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if policy in ("denied", "denied-exit0"):
        assert (tmp_path / "provisioned").read_text() == "provisioning\n"
    else:
        assert "sudo" in result.stderr
        assert not (tmp_path / "provisioned").exists()
