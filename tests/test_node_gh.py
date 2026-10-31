"""GitHub CLI prerequisites and broker failures; no live login or installation."""
import os
import subprocess
from pathlib import Path

import pytest
from test_linux_distros import system  # noqa: F401

from acc.broker.logins import LoginError, Logins

REPO = Path(__file__).resolve().parents[1]


def test_missing_github_cli_is_an_actionable_login_error(env, tmp_path):
    env['broker'].providers['gh'] = str(tmp_path / 'missing-gh')
    with pytest.raises(LoginError, match='gh is not installed:'):
        Logins(env['broker']).start('github')


@pytest.mark.parametrize('manager,package', [('apt', 'gh'), ('dnf', 'gh'),
                                             ('pacman', 'github-cli'), ('zypper', 'gh')])
def test_node_reports_missing_gh_with_native_hint(system, manager, package):  # noqa: F811
    from test_linux_distros import node_dry_run
    path = system('apt-get' if manager == 'apt' else manager)
    out = node_dry_run(path, '--unqualified-distro')
    assert f'tmux git {package}' in out.stdout
    if manager == 'dnf':
        assert out.stdout.index('ca-certificates curl gnupg2') < out.stdout.index('gh-rpm-repo.sh')
        assert out.stdout.index('gh-rpm-repo.sh') < out.stdout.index('tmux git gh')
    # Same dry-run prerequisite discovery without the install flag.
    from test_linux_distros import REPO as ROOT
    from test_linux_distros import run_installer
    result = run_installer(['bash', str(ROOT / 'deploy/node/install.sh'), '--dry-run', '--agent', 'alex-agent',
                             '--unqualified-distro'], capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'PATH': path})
    assert 'missing tmux git gh:' in result.stdout
    assert f'tmux git {package}' in result.stdout
    assert '--install-prereqs' in result.stdout


def test_mac_node_installs_missing_gh_with_homebrew(tmp_path):
    source = (REPO / 'deploy/node/macos/install.sh').read_text()
    fragment = source[source.index('if command -v tmux'):source.index('UV=$(command -v uv')]
    calls = tmp_path / 'calls'
    result = subprocess.run(['bash', '-eu', '-c', '''command() { return 1; }
ok() { :; }
run() { printf '%s\\n' "$*" >> "$CALLS"; }
''' + fragment], capture_output=True, text=True,
                            env={**os.environ, 'BREW': 'brew', 'CALLS': str(calls)})
    assert result.returncode == 0, result.stderr
    assert 'brew install gh' in calls.read_text().splitlines()
