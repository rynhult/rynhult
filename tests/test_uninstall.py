"""deploy/uninstall.sh in a fake root with fake system tools: never this machine's own services, files or accounts."""
import hashlib
import json
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'deploy/uninstall.sh'
PHRASE = 'delete my Rynhult data'


def write(path: Path, text='x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def link(path: Path, target: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


@pytest.fixture
def unprivileged(tmp_path):
    """Where the fake machine lives and who runs the script. The script refuses a test root as root (rightly), and the
    gates run as root: then it runs as an unprivileged account (ACC_TEST_UNPRIVILEGED_USER, default nobody) through
    runuser, in a fresh /tmp folder that account owns, since root's private test folders aren't reachable for it."""
    if os.geteuid() != 0:
        yield tmp_path, None
        return
    user = os.environ.get('ACC_TEST_UNPRIVILEGED_USER', 'nobody')
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        pytest.skip(f'running as root, and there is no {user!r} account to run the uninstaller as '
                    '(set ACC_TEST_UNPRIVILEGED_USER)')
    if account.pw_uid == 0 or not shutil.which('runuser'):
        pytest.skip(f'running as root without runuser or an unprivileged {user!r} account to run the uninstaller as')
    base = Path(tempfile.mkdtemp(prefix='rhun.', dir='/tmp'))
    try:
        yield base, account
    finally:
        shutil.rmtree(base, ignore_errors=True)


def hand_over(base: Path, account):
    """Everything the script and its fake tools touch belongs to the account that runs them."""
    for path in (base, *base.rglob('*')):
        # Absolute installed-layout links are inert fixture entries. Their parent
        # is handed over, so the account can unlink them without owning the link.
        # Never ask a path guard (or a future ownership helper) to resolve them.
        if not path.is_symlink():
            os.lchown(path, account.pw_uid, account.pw_gid)


@pytest.fixture
def machine(unprivileged):
    """An installed main Rynhult as the installers lay it out, plus things that aren't Rynhult's."""
    tmp_path, account = unprivileged
    root = tmp_path / 'root'
    units = root / 'etc/systemd/system'
    for name in ('accd.service', 'accd.socket', 'acc-broker@.service', 'acc-broker@.socket', 'acc-update.path',
                 'acc-update.service', 'acc-backup.timer', 'acc-launcher.slice', 'acc-containers@.service'):
        write(units / name, '[Unit]\n')
    link(units / 'rynhultd.service', '/etc/systemd/system/accd.service')
    link(units / 'sockets.target.wants/accd.socket', '/etc/systemd/system/accd.socket')
    link(units / 'multi-user.target.wants/ssh.service', '/usr/lib/systemd/system/ssh.service')  # not Rynhult's
    write(units / 'acc-backup.service.d/mirrors.conf')
    for dropin in ('acc.conf', 'hardening.conf'):
        write(units / 'caddy.service.d' / dropin)
    write(units / 'nginx.service')  # not Rynhult's
    write(root / 'etc/caddy/Caddyfile', '{\n\tadmin unix//run/caddy/admin.sock\n}\n\nimport acc.d/*.caddy\n')
    write(root / 'etc/caddy/acc.d/acc.caddy')
    write(root / 'etc/caddy/cloudflare.env', 'CF_API_TOKEN=synthetic')
    write(root / 'etc/apparmor.d/acc-chrome')
    write(root / 'etc/apparmor.d/usr.bin.firefox')  # not Rynhult's
    write(root / 'etc/default/accd', 'ACC_SERVICES_ACCOUNT=alex\n')
    write(root / 'etc/acc/broker/alex-agent.env', 'ACC_BROKER_ROLE=agents\n')
    write(root / 'etc/acc/broker/sam-agent.env', 'ACC_BROKER_ROLE=agents\n')
    write(root / 'etc/acc/broker/alex.env', 'ACC_BROKER_ROLE=services\n')  # the owner's own account: never removed
    # The system's AccountsService, not Rynhult's (security review 10, SR10-2)
    link(units / 'graphical.target.wants/accounts-daemon.service', '/usr/lib/systemd/system/accounts-daemon.service')
    write(units / 'accounts-daemon.service.d/override.conf')
    write(units / 'accounting.service', '[Unit]\n')
    for name in ('acc-update', 'acc-backup', 'acc_env.py', 'acc-agent-add'):
        write(root / 'usr/local/sbin' / name)
    link(root / 'usr/local/sbin/rynhult-backup', '/usr/local/sbin/acc-backup')
    write(root / 'usr/local/bin/acc-env.sh')
    link(root / 'usr/local/bin/rynhult', '/srv/acc/app/.venv/bin/rynhult')
    link(root / 'usr/local/bin/acc-request', '/srv/acc/bin/acc-request')
    link(root / 'usr/local/bin/rynhult-tool', '/opt/elsewhere/tool')  # a link Rynhult didn't make
    write(root / 'usr/local/bin/claude', 'a separately installed CLI')
    write(root / 'usr/local/lib/acc-dns/credentials.py')
    write(root / 'var/log/acc-install.log')
    for folder in ('app/pyproject.toml', 'bin/acc-guard', 'run/alex-agent/spool', '.uv-cache/x', 'catalog/x'):
        write(root / 'srv/acc' / folder)
    write(root / 'srv/acc/data/acc.db', 'database')
    write(root / 'srv/acc/git/web-shop.git/HEAD')
    write(root / 'srv/acc/workspaces/web-shop/README.md', 'my project')
    write(root / 'var/backups/acc/2026-10-27/acc.db', 'backup')

    tools = tmp_path / 'tools'
    tools.mkdir()
    calls = tmp_path / 'calls.jsonl'
    for tool in ('systemctl', 'userdel', 'apparmor_parser'):
        (tools / tool).write_text(f'''#!/usr/bin/env python3
import json, sys
with open({str(calls)!r}, "a") as log:
    log.write(json.dumps([{tool!r}, *sys.argv[1:]]) + "\\n")
''')
        (tools / tool).chmod(0o755)
    (tools / 'id').write_text('''#!/usr/bin/env python3
import os, sys
if sys.argv[1:] == ["-u"]:
    print(os.getuid()); sys.exit(0)
sys.exit(0 if sys.argv[-1] in os.environ.get("FAKE_USERS", "").split() else 1)
''')
    (tools / 'ps').write_text('''#!/usr/bin/env python3
import os, sys
print("4242" if sys.argv[-1] in os.environ.get("FAKE_BUSY", "").split() else "")
''')
    for tool in ('id', 'ps'):
        (tools / tool).chmod(0o755)
    script = SCRIPT
    if account:
        script = tmp_path / 'uninstall.sh'  # the account may not be able to read the checkout
        shutil.copy2(SCRIPT, script)
        hand_over(tmp_path, account)

    def run(*args, stdin='', users='acc acc-build alex alex-agent sam-agent', busy=''):
        env = {'PATH': f"{tools}:{os.environ['PATH']}", 'ACC_UNINSTALL_ROOT': str(root), 'FAKE_USERS': users,
               'FAKE_BUSY': busy, 'HOME': str(tmp_path), 'LANG': 'C.UTF-8'}
        command = ['bash', str(script), *args]
        if account:  # as root: the same run as the unprivileged account, with exactly this environment
            command = ['runuser', '-u', account.pw_name, '--', 'env', '-i', *(f'{k}={v}' for k, v in env.items()), *command]
        result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=60, cwd=tmp_path,
                                env={**os.environ, **env})
        logged = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, logged

    return root, run


def tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def test_dry_run_lists_everything_and_changes_nothing(machine):
    root, run = machine
    before = tree(root)
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert tree(root) == before and calls == []
    out = result.stdout
    for line in ('would stop and disable accd.service', 'would stop acc-broker@*.service',
                 'would remove /etc/systemd/system/accd.service', 'would remove /etc/caddy/acc.d',
                 "would remove the line 'import acc.d/*.caddy' from /etc/caddy/Caddyfile",
                 'would unload and remove /etc/apparmor.d/acc-chrome', 'would remove /usr/local/bin/rynhult',
                 'would remove /srv/acc/app', 'would delete account acc (its home folder stays)',
                 'would delete account sam-agent (its home folder stays)', 'no sudoers files'):
        assert line in out
    for kept in ('/srv/acc/workspaces', '/srv/acc/data', '/srv/acc/git', '/var/backups/acc', 'nginx', 'ssh.service',
                 'firefox', '/usr/local/bin/claude', 'rynhult-tool'):
        assert f'would remove {kept}' not in out
    assert 'Dry run finished: nothing was changed.' in out


def test_yes_removes_rynhult_and_keeps_data_and_what_isnt_rynhults(machine):
    root, run = machine
    result, calls = run('--yes')
    assert result.returncode == 0, result.stderr
    units = root / 'etc/systemd/system'
    for gone in (units / 'accd.service', units / 'rynhultd.service', units / 'acc-broker@.service',
                 units / 'sockets.target.wants/accd.socket', units / 'acc-backup.service.d', units / 'caddy.service.d',
                 root / 'etc/caddy/acc.d', root / 'etc/caddy/cloudflare.env', root / 'etc/apparmor.d/acc-chrome',
                 root / 'etc/default/accd', root / 'etc/acc', root / 'usr/local/sbin/acc-update',
                 root / 'usr/local/sbin/rynhult-backup', root / 'usr/local/bin/rynhult', root / 'usr/local/bin/acc-request',
                 root / 'usr/local/bin/acc-env.sh', root / 'usr/local/lib/acc-dns', root / 'var/log/acc-install.log',
                 root / 'srv/acc/app', root / 'srv/acc/bin', root / 'srv/acc/run', root / 'srv/acc/.uv-cache'):
        assert not exists(gone), gone
    for kept in (units / 'nginx.service', units / 'multi-user.target.wants/ssh.service',
                 units / 'graphical.target.wants/accounts-daemon.service', units / 'accounts-daemon.service.d',
                 units / 'accounting.service',
                 root / 'etc/apparmor.d/usr.bin.firefox', root / 'usr/local/bin/claude', root / 'usr/local/bin/rynhult-tool',
                 root / 'srv/acc/data/acc.db', root / 'srv/acc/git/web-shop.git/HEAD',
                 root / 'srv/acc/workspaces/web-shop/README.md', root / 'var/backups/acc/2026-10-27/acc.db'):
        assert exists(kept), kept
    caddyfile = (root / 'etc/caddy/Caddyfile').read_text()
    assert 'import acc.d' not in caddyfile and 'admin unix//run/caddy/admin.sock' in caddyfile
    assert ['systemctl', 'disable', '--now', 'accd.service'] in calls
    assert ['systemctl', 'stop', 'acc-broker@*.service'] in calls and ['systemctl', 'stop', 'acc-containers@*.service'] in calls
    assert ['systemctl', 'daemon-reload'] in calls and ['systemctl', 'try-restart', 'caddy.service'] in calls
    assert ['apparmor_parser', '-R', str(root / 'etc/apparmor.d/acc-chrome')] in calls
    assert not any(c[0] == 'systemctl' and ('nginx.service' in c or 'accounting.service' in c) for c in calls)
    deleted = sorted(c[1:] for c in calls if c[0] == 'userdel')
    assert deleted == [['acc'], ['acc-build'], ['alex-agent'], ['sam-agent']]  # without -r: homes stay


def test_an_account_with_processes_is_skipped_not_killed(machine):
    root, run = machine
    result, calls = run('--yes', busy='alex-agent')
    assert result.returncode == 0, result.stderr
    assert 'skipped alex-agent: it still runs processes' in result.stdout
    assert ['userdel', 'alex-agent'] not in calls and ['userdel', 'sam-agent'] in calls


@pytest.mark.parametrize('typed', ['', 'yes', 'delete my rynhult data'])
def test_purge_needs_the_exact_phrase_and_changes_nothing_without_it(machine, typed):
    root, run = machine
    before = tree(root)
    result, calls = run('--yes', '--purge-data', stdin=typed + '\n')
    assert result.returncode == 1 and 'Not confirmed: nothing was changed.' in result.stderr
    assert tree(root) == before and calls == []


def test_purge_with_the_phrase_also_deletes_data_backups_and_agent_homes(machine):
    root, run = machine
    result, calls = run('--yes', '--purge-data', stdin=PHRASE + '\n')
    assert result.returncode == 0, result.stderr
    assert not exists(root / 'srv/acc') and not exists(root / 'var/backups/acc')
    assert ['userdel', '-r', 'alex-agent'] in calls and ['userdel', '-r', 'acc'] in calls
    assert exists(root / 'usr/local/bin/claude') and exists(root / 'etc/systemd/system/nginx.service')


def test_purge_dry_run_lists_data_without_asking(machine):
    root, run = machine
    before = tree(root)
    result, _ = run('--purge-data')
    assert result.returncode == 0, result.stderr
    assert 'would remove /srv/acc\n' in result.stdout and 'would remove /var/backups/acc' in result.stdout
    assert 'Type' not in result.stdout and tree(root) == before


@pytest.mark.parametrize('value', ['relative/root', '/'])
def test_a_test_root_must_be_an_absolute_folder_other_than_slash(value):
    result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True,
                            env={**os.environ, 'ACC_UNINSTALL_ROOT': value})
    assert result.returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason='checks the non-root refusal')
def test_yes_on_the_real_machine_needs_root():
    env = {k: v for k, v in os.environ.items() if k != 'ACC_UNINSTALL_ROOT'}
    result = subprocess.run(['bash', str(SCRIPT), '--yes'], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and 'run as root' in result.stderr


@pytest.mark.skipif(shutil.which('shellcheck') is None, reason='needs shellcheck')
def test_uninstaller_passes_shellcheck():
    subprocess.run(['shellcheck', str(SCRIPT)], check=True)


@pytest.fixture
def simulated_root(monkeypatch, tmp_path_factory):
    """Root's code path, run as this account: a fake runuser that drops `-u USER --` and logs USER, and no chown."""
    tools = tmp_path_factory.mktemp('runuser')
    log = tools / 'runuser.log'
    (tools / 'runuser').write_text(f'#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 64\necho "$2" >> {log}\n'
                                   'shift 3\nexec "$@"\n')
    (tools / 'runuser').chmod(0o755)
    monkeypatch.setenv('PATH', f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'lchown', lambda *a: None)
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: pwd.struct_passwd(
        (name, 'x', 65534, 65534, '', '/nonexistent', '/usr/sbin/nologin')))
    return log


@pytest.mark.skipif(os.geteuid() == 0, reason='real root: every other test here already runs the script through the real '
                    'runuser as the unprivileged account; this simulation would run it as root')
def test_as_root_the_script_runs_as_an_unprivileged_account(simulated_root, machine):
    """Root's path without being root: the fake runuser only logs the account and can't drop privileges."""
    root, run = machine
    assert str(root).startswith('/tmp/rhun.')  # a folder that account can reach, not root's private test folder
    result, calls = run()
    assert result.returncode == 0, result.stderr
    refused, _ = run('--yes', '--purge-data', stdin='no\n')
    assert refused.returncode == 1 and 'Not confirmed' in refused.stderr
    assert simulated_root.read_text().split() == ['nobody', 'nobody']


def test_handing_over_the_fake_root_never_resolves_installed_command_links(tmp_path):
    # Exercise real lchown auditing even without root: the root-only fixture path
    # otherwise gets hidden by simulated_root's lchown stub.
    root = tmp_path / 'fake-machine'
    write(root / 'srv/acc/bin/acc-request', 'fake command')
    command = root / 'usr/local/bin/acc-request'
    link(command, '/srv/acc/bin/acc-request')
    account = pwd.struct_passwd(('test-account', 'x', os.getuid(), os.getgid(), '', str(root), '/bin/false'))
    hand_over(root, account)
    assert os.readlink(command) == '/srv/acc/bin/acc-request'
    assert (root / 'srv/acc/bin/acc-request').read_text() == 'fake command'


def test_uninstall_existence_check_does_not_probe_a_symlinks_target(tmp_path):
    root = tmp_path / 'fake-root'
    root.mkdir()
    target = tmp_path / 'outside-fake-root'
    target.write_text('outside')
    (root / 'command').symlink_to(target)
    function = next(line for line in SCRIPT.read_text().splitlines() if line.startswith('gone()'))
    result = subprocess.run(['bash', '-xc', function + '\nR=$1; gone /command', 'probe', str(root)],
                            capture_output=True, text=True, cwd=tmp_path, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    trace = [shlex.split(line.lstrip('+ ')) for line in result.stderr.splitlines()]
    probes = [args for args in trace if args and args[0] == '[']
    assert ['[', '-L', str(root / 'command'), ']'] in probes
    assert ['[', '-e', str(root / 'command'), ']'] not in probes


def test_the_owners_own_account_is_never_deleted(machine):
    """Setup writes a broker file for the owner's account too (services role): never on the delete list, even with
    --purge-data and nothing running as it (security review 10, SR10-1)."""
    root, run = machine
    result, calls = run('--yes', '--purge-data', stdin=PHRASE + '\n')
    assert result.returncode == 0, result.stderr
    assert not any(c[0] == 'userdel' and 'alex' in c for c in calls)
    assert ['userdel', '-r', 'alex-agent'] in calls
    dry, _ = run()
    assert 'account alex ' not in dry.stdout and 'skipped alex:' not in dry.stdout


def test_the_services_account_is_kept_even_with_an_agent_role_file(machine):
    root, run = machine
    (root / 'etc/acc/broker/alex.env').write_text('ACC_BROKER_ROLE=agents\n')  # a hand-edited or stale file
    result, calls = run('--yes')
    assert result.returncode == 0, result.stderr
    assert ['userdel', 'alex'] not in calls and ['userdel', 'alex-agent'] in calls
