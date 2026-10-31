"""Installer substeps execute against owned scratch only, without accounts or services."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_broker_git_setup_ignores_launcher_home_and_git_environment(tmp_path):
    source = (REPO / 'deploy/broker/acc-agent-add').read_text()
    fragment = source[source.index('  # Project checkouts'):source.index('\nfi\n\nlimits=')]
    account_home = tmp_path / 'agent'
    account_home.mkdir()
    caller = tmp_path / 'launcher'
    caller.mkdir()
    (caller / '.git').write_text('not a git repository pointer')
    inherited = caller / 'caller-config'
    inherited.write_text('[safe]\n\tdirectory = inherited-only\n')
    script = 'runuser() { shift 2; [ "$1" = -- ]; shift; "$@"; }\n' + fragment
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=caller, capture_output=True, text=True,
                            env={**os.environ, 'account': 'alex-agent', 'home': str(account_home),
                                 'HOME': str(caller), 'GIT_DIR': str(caller / '.git'),
                                 'GIT_CONFIG_GLOBAL': str(inherited), 'GIT_CONFIG_COUNT': '1',
                                 'GIT_CONFIG_KEY_0': 'safe.directory', 'GIT_CONFIG_VALUE_0': 'injected'}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert inherited.read_text() == '[safe]\n\tdirectory = inherited-only\n'
    config = account_home / '.gitconfig'
    assert config.exists()
    assert 'directory = *' in config.read_text()
    assert not (account_home / '.git').exists()


def test_external_setup_never_executes_same_version_stale_install(tmp_path):
    source = (REPO / 'deploy/setup.sh').read_text()
    fragment = source[source.index('if [ "$REPO" != "$APP" ]'):source.index('\nVERSION=')]
    new, installed = tmp_path / 'new', tmp_path / 'installed'
    (new / 'deploy').mkdir(parents=True)
    (new / 'deploy/setup.sh').write_text('# newer same-version setup\n')
    (installed / 'deploy').mkdir(parents=True)
    for tree in (new, installed):
        (tree / 'pyproject.toml').write_text('version = "0.15.0-rc.1"\n')
    trace = tmp_path / 'stale-was-executed'
    script = installed / 'deploy/setup.sh'
    script.write_text('#!/bin/sh\ntouch "$TRACE"\n')
    script.chmod(0o755)
    result = subprocess.run(['bash', '-eu', '-c', 'die() { echo "$*" >&2; exit 1; }; log() { :; };\n' + fragment],
                            capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'REPO': str(new), 'APP': str(installed), 'DRY': '', 'TRACE': str(trace)})
    assert result.returncode != 0
    assert not trace.exists()
    assert 'installed checkout' in result.stderr
    assert 'Updating' in result.stderr


def test_broad_sudo_policy_stays_refused_with_actionable_exclusion(tmp_path):
    source = (REPO / 'deploy/broker/acc-agent-add').read_text()
    fragment = source[source.index('if [ "$role" = agents ]; then'):source.index('\nhome=$(getent')]
    script = '''id() { echo users; }
sudo() { echo "User alex-agent may run the following commands on this host:"; echo "    (ALL) ALL"; }
''' + fragment
    result = subprocess.run(['bash', '-eu', '-c', script], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'role': 'agents', 'account': 'alex-agent'})
    assert result.returncode != 0
    assert 'sudo authorization' in result.stdout + result.stderr
    assert 'RYNHULT_AGENTS' in result.stdout + result.stderr
    assert 'visudo -c' in result.stdout + result.stderr

    assert 'copy /usr/etc/sudoers to /etc/sudoers' in result.stderr
