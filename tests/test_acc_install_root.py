"""deploy/acc-install for real (not a dry run) in a fake root: ACC_INSTALL_ROOT keeps its log, the existing-setup check
and acc-node inside a temporary folder, and the installers it runs are stubs in a copy of deploy/. Never this machine's
own files, services or accounts; the seam is refused as root."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CODE = '1a2b3c-4d5e6f-7a8b9c-0d1e2f'
TOKEN = 'cf-synthetic-0123456789abcdef'  # gitleaks:allow -- fixed fake credential passed only to installer stubs
SETUP_STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_CALLS"
printf 'owner=%s zone=%s token=%s clis=%s backup=%s\\n' "$ACC_OWNER" "$ACC_ZONE" "$CF_API_TOKEN" "$ACC_AGENT_CLIS" \\
  "$ACC_BACKUP" >> "$STUB_CALLS"
echo "== packages"
echo "using Cloudflare token $CF_API_TOKEN"
echo "Authorization: Bearer $CF_API_TOKEN"
[ -z "${STUB_FAIL:-}" ] || { echo "== caddy"; echo "E: Unable to locate package caddy"; exit "$STUB_FAIL"; }
echo "== done"
'''
NODE_INSTALL_STUB = '''#!/usr/bin/env bash
printf 'install %s enrolls=%s\\n' "$*" "$ACC_INSTALL_ENROLLS" >> "$STUB_CALLS"
echo "node prerequisites"
exit "${STUB_FAIL:-0}"
'''
ACC_NODE_STUB = '''#!/usr/bin/env bash
IFS= read -r code
printf 'acc-node %s stdin=%s\\n' "$*" "$code" >> "$STUB_CALLS"
echo "enrolling with the main Rynhult"
exit "${STUB_ENROLL_FAIL:-0}"
'''


def executable(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def machine(tmp_path):
    """A copy of the wizard with stub installers, an empty fake root, and a runner."""
    repo = tmp_path / 'repo'
    (repo / 'deploy/lib').mkdir(parents=True)
    shutil.copy2(REPO / 'deploy/acc-install', repo / 'deploy/acc-install')
    shutil.copy2(REPO / 'deploy/lib/acc-env.sh', repo / 'deploy/lib/acc-env.sh')
    executable(repo / 'deploy/setup.sh', SETUP_STUB)
    executable(repo / 'deploy/node/install.sh', NODE_INSTALL_STUB)
    root = tmp_path / 'root'
    (root / 'var/log').mkdir(parents=True)
    root.chmod(0o700)
    calls = tmp_path / 'calls'

    credentials = {}
    if os.geteuid() == 0:
        # No real account changes: drop only this child's credentials. Expose only the test's /tmp tree.
        for parent in tmp_path.parents:
            if parent == Path('/tmp'):
                break
            assert parent.is_relative_to('/tmp')
            parent.chmod(stat.S_IMODE(parent.stat().st_mode) | 0o111)
        for path in [tmp_path, *tmp_path.rglob('*')]:
            os.chown(path, 65534, 65534, follow_symlinks=False)
        credentials = {'user': 65534, 'group': 65534, 'extra_groups': []}

    def run(*args, stdin='', as_real_root=False, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('ACC_', 'RYNHULT_', 'CF_')) and k not in {'BASH_ENV', 'ENV'}}
        clean.update({'ACC_INSTALL_ROOT': str(root), 'STUB_CALLS': str(calls), 'SUDO_USER': '', **env})
        return subprocess.run(['bash', str(repo / 'deploy/acc-install'), *args], input=stdin, capture_output=True,
                              text=True, timeout=60, env=clean, cwd=tmp_path,
                              **({} if as_real_root else credentials))
    return run, root, calls, repo


def log_of(root):
    return root / 'var/log/acc-install.log'


def test_fresh_main_install_runs_setup_with_the_answers_and_keeps_a_private_redacted_log(machine):
    run, root, calls, repo = machine
    result = run('--main', '--yes', ACC_OWNER='alex', ACC_ZONE='example.com', CF_API_TOKEN=TOKEN)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.read_text().splitlines()
    assert lines[0] == '--yes'  # no --dry-run: this is the real step
    assert lines[1] == f'owner=alex zone=example.com token={TOKEN} clis=codex backup=yes'  # defaults for the rest
    log = log_of(root)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text()
    assert '== packages' in text and '== done' in text
    assert TOKEN not in text and TOKEN not in result.stdout
    assert 'Bearer [redacted]' in text
    assert 'Rynhult is installed' in result.stdout
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*')) == ['var', 'var/log', 'var/log/acc-install.log']


def test_rerun_on_an_installed_machine_keeps_its_settings_and_is_idempotent(machine):
    run, root, calls, _ = machine
    (root / 'etc/default').mkdir(parents=True)
    (root / 'etc/default/accd').write_text('ACC_SERVICES_ACCOUNT=alex\n')
    log = log_of(root)
    log.write_text('the previous run: keep nothing of it\n')
    log.chmod(0o644)
    outputs = []
    for _ in range(2):
        result = run('--main', '--yes')
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(result.stdout)
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        assert 'previous run' not in log.read_text()
    assert outputs[0] == outputs[1]
    assert 'already set up on this machine' in outputs[0]
    # No questions asked and no defaults filled in: setup.sh keeps what /etc/default/accd says.
    assert calls.read_text().splitlines() == ['--yes', 'owner= zone= token= clis= backup='] * 2
    assert (root / 'etc/default/accd').read_text() == 'ACC_SERVICES_ACCOUNT=alex\n'


def test_the_log_is_never_written_through_a_planted_link(machine, tmp_path):
    run, root, _, _ = machine
    victim = tmp_path / 'victim'
    victim.write_text('untouched\n')
    (root / 'var/log/acc-install.log.new').symlink_to(victim)
    log_of(root).symlink_to(victim)
    result = run('--main', '--yes')
    assert result.returncode == 2 and 'symlink' in result.stderr
    assert victim.read_text() == 'untouched\n'
    assert log_of(root).is_symlink()


def test_dry_run_changes_nothing_in_the_root_and_tells_setup(machine):
    run, root, calls, _ = machine
    result = run('--main', '--yes', '--dry-run')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '(dry run: nothing is changed)' in result.stdout
    assert calls.read_text().splitlines()[0] == '--yes --dry-run'
    assert list(root.rglob('*')) == [root / 'var', root / 'var/log']
    assert 'Rynhult is installed' not in result.stdout


def test_a_setup_that_fails_midway_stops_with_its_status_the_last_lines_and_the_log(machine):
    run, root, _, _ = machine
    result = run('--main', '--yes', ACC_ZONE='example.com', CF_API_TOKEN=TOKEN, STUB_FAIL='3')
    assert result.returncode == 3
    out = result.stdout
    assert f'Setup stopped with an error (exit 3). The log is in {log_of(root)}' in out
    assert 'E: Unable to locate package caddy' in out.split('Setup stopped', 1)[1]  # the context, after the message
    assert f'Full log: {log_of(root)}' in out
    assert 'Rynhult is installed' not in out and TOKEN not in out + log_of(root).read_text()


def test_node_install_then_enroll_with_the_code_only_on_stdin(machine):
    run, root, calls, _ = machine
    executable(root / 'usr/local/bin/acc-node', ACC_NODE_STUB)
    command = 'rynhult-node enroll --primary https://acc.example.lan --trust system --code - --name pc'
    result = run('--node', command, '--yes', stdin=CODE + '\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == [
        'install --install-prereqs enrolls=1',
        f'acc-node enroll --primary https://acc.example.lan --trust system --code - --name pc stdin={CODE}']
    assert 'enrolled as pc' in result.stdout
    text = log_of(root).read_text()
    assert 'node prerequisites' in text and 'enrolling with the main Rynhult' in text
    assert CODE not in text + result.stdout + result.stderr


def test_a_failed_node_install_never_enrolls(machine):
    run, root, calls, _ = machine
    executable(root / 'usr/local/bin/acc-node', ACC_NODE_STUB)
    command = f'acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name pc'
    result = run('--node', command, '--yes', STUB_FAIL='4')
    assert result.returncode == 4
    assert 'Installing the node stopped with an error (exit 4)' in result.stdout
    assert all(not line.startswith('acc-node') for line in calls.read_text().splitlines())


def test_a_failed_enroll_says_how_to_get_a_new_code(machine):
    run, root, _, _ = machine
    executable(root / 'usr/local/bin/acc-node', ACC_NODE_STUB)
    command = f'acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name pc'
    result = run('--node', command, '--yes', STUB_ENROLL_FAIL='5')
    assert result.returncode == 5
    assert 'Enrolling stopped with an error (exit 5). A code works once and for 10 minutes' in result.stdout
    assert CODE not in log_of(root).read_text()


@pytest.mark.parametrize('value, error', [
    ('relative/root', 'must be an absolute folder'),
    ('/', 'private folder under /tmp'),
    ('MISSING', 'an existing folder other than /'),
])
def test_the_test_root_must_be_a_real_folder(machine, value, error):
    run, root, calls, _ = machine
    if value == 'MISSING':
        value = str(root.parent / 'missing')
    result = run('--main', '--yes', ACC_INSTALL_ROOT=value)
    assert result.returncode == 2 and error in result.stderr
    assert not calls.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason='actual-root refusal runs in the coordinator root gate')
def test_the_test_root_is_refused_as_root(machine, tmp_path):
    run, root, calls, _ = machine
    tools = tmp_path / 'tools'
    # Root must be refused even if PATH lies about its identity.
    executable(tools / 'id', '#!/bin/sh\necho 1000\n')
    result = run('--main', '--yes', as_real_root=True, PATH=f'{tools}:{os.environ["PATH"]}')
    assert result.returncode == 2 and 'refused as root' in result.stderr
    assert not calls.exists() and not log_of(root).exists()


def test_without_the_test_root_a_real_install_still_needs_root(machine):
    run, root, calls, _ = machine
    result = run('--main', '--yes', ACC_INSTALL_ROOT='')
    assert result.returncode == 1 and 'run as root' in result.stderr
    assert not calls.exists()


def test_help_shows_the_usage_and_nothing_else():
    result = subprocess.run(['bash', str(REPO / 'deploy/acc-install'), '--help'], capture_output=True, text=True)
    lines = result.stdout.splitlines()
    assert lines[0].startswith('Install Rynhult on Linux') and lines[-1].startswith('  --dry-run')
    assert any(line.startswith('  --yes') for line in lines) and 'shellcheck' not in result.stdout


# ---- verified downloads ------------------------------------------------------------------------------------------------

PINNED = '9DC858229FC7DD38854AE2D88D81803C0EBFCD88'


@pytest.fixture
def keys(tmp_path):
    tools, calls = tmp_path / 'tools', tmp_path / 'curl-args'
    executable(tools / 'curl', f'''#!/bin/sh
printf '%s\\n' "$*" >> "{calls}"
while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; printf 'downloaded key' > "$1"; fi; shift; done
''')

    def run(fingerprint, destination):
        executable(tools / 'gpg', f'''#!/bin/sh
case "$*" in
  *--show-keys*) printf 'pub::::::::::\\nfpr:::::::::{fingerprint}:\\n' ;;
  *--dearmor*) while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; printf 'dearmored' > "$1"; fi; shift; done ;;
esac
''')
        return subprocess.run(['sh', str(REPO / 'deploy/lib/install-apt-key.sh'), 'https://keys.example.com/key.asc',
                               str(destination), PINNED], capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}'})
    return run, calls


@pytest.mark.parametrize('name', ['vendor.gpg', 'vendor.asc'])
def test_a_rejected_apt_key_never_replaces_the_live_keyring(keys, tmp_path, name):
    run, calls = keys
    live = tmp_path / 'keyrings' / name
    live.parent.mkdir()
    live.write_text('the key that was verified last time')
    result = run('BADBADBAD', live)
    assert result.returncode == 1 and 'fingerprint mismatch' in result.stderr
    assert live.read_text() == 'the key that was verified last time'
    assert '--proto =https' in calls.read_text()


@pytest.mark.parametrize('name, content', [('vendor.gpg', 'dearmored'), ('vendor.asc', 'downloaded key')])
def test_a_verified_apt_key_is_installed_world_readable(keys, tmp_path, name, content):
    run, _ = keys
    live = tmp_path / name
    result = run(PINNED, live)
    assert result.returncode == 0, result.stderr
    assert live.read_text() == content and stat.S_IMODE(live.stat().st_mode) == 0o644


def test_setup_installs_every_apt_key_through_the_staged_check():
    # setup.sh fetched Caddy's key straight into the live keyring and checked it afterwards: a rejected key had already
    # replaced the file. Every signing key now goes through install-apt-key.sh (staging, pinned primary fingerprint).
    setup = (REPO / 'deploy/setup.sh').read_text()
    assert 'gpg --dearmor' not in setup and '| gpg' not in setup
    assert 'install-apt-key.sh" https://dl.cloudsmith.io/public/caddy/stable/gpg.key "$CADDY_KEYRING"' in setup


@pytest.mark.parametrize('component', ['root', 'var', 'var/log', 'usr/local/bin/acc-node', 'etc/default/accd'])
def test_test_root_refuses_symlink_escapes(machine, tmp_path, component):
    run, root, calls, _ = machine
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'acc-install.log'
    victim.write_text('preserve')
    if component == 'root':
        link = tmp_path / 'root-link'
        link.symlink_to(root, target_is_directory=True)
        result = run('--main', '--yes', ACC_INSTALL_ROOT=str(link))
    else:
        target = root / component
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(victim if component in {'usr/local/bin/acc-node', 'etc/default/accd'} else outside)
        result = run('--main', '--yes')
    assert result.returncode == 2, result.stdout + result.stderr
    assert not calls.exists()
    assert victim.read_text() == 'preserve'


@pytest.mark.parametrize('value', ['/etc', '/usr', '/var', '/run/acc', '/srv/acc', '/tmp/../etc'])
def test_test_root_refuses_system_paths_before_installation(machine, value):
    run, root, calls, _ = machine
    result = run('--main', '--yes', ACC_INSTALL_ROOT=value)
    assert result.returncode == 2
    assert not calls.exists() and not log_of(root).exists()


def test_test_root_requires_private_permissions(machine):
    run, root, calls, _ = machine
    root.chmod(0o755)
    result = run('--main', '--yes')
    assert result.returncode == 2 and '0700' in result.stderr
    assert not calls.exists()


def test_test_root_identity_does_not_trust_path_id(machine, tmp_path):
    run, _, calls, _ = machine
    tools = tmp_path / 'identity-tools'
    executable(tools / 'id', '#!/bin/sh\necho 0\n')
    result = run('--main', '--yes', PATH=f'{tools}:{os.environ["PATH"]}')
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.exists()
