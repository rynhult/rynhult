"""Phase B is additive and rollback owns only links it created, on offline hosts."""
import os
import runpy
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'deploy/lib/public-aliases.py'


def api():
    return runpy.run_path(str(SCRIPT))


def put(root, name, content='#!/bin/sh\nexit 0\n'):
    path = root / name.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, os.readlink(p) if p.is_symlink()
            else p.read_bytes() if p.is_file() else None) for p in root.rglob('*')}


@pytest.mark.parametrize('role', ['main', 'node'])
@pytest.mark.parametrize('legacy', [False, True])
def test_install_dryrun_update_rerun_rollback(tmp_path, role, legacy):
    (tmp_path / 'etc').mkdir(exist_ok=True)
    m = api()
    pairs = m['command_aliases'](role)
    units = tmp_path / 'etc/systemd/system'
    units.mkdir(parents=True)
    put(tmp_path, '/etc/systemd/system/acc-broker@.service', '[Service]\nKillMode=process\n')
    for _, target in pairs:
        put(tmp_path, target)
    # A phase A / operator-created alias must survive phase B rollback.
    preexisting, target = pairs[0]
    alias = tmp_path / preexisting.lstrip('/')
    alias.parent.mkdir(parents=True, exist_ok=True)
    if legacy:
        alias.symlink_to(os.path.relpath(tmp_path / target.lstrip('/'), alias.parent))
    installer = REPO / ('deploy/install.sh' if role == 'main' else 'deploy/node/install.sh')

    def installer_plan(*flags):
        result = subprocess.run(['bash', str(installer), '--aliases-only', '--dry-run',
                                 '--alias-root', str(tmp_path), *flags], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    before = snapshot(tmp_path)
    assert any(line.startswith('alias /') for line in installer_plan())
    m['reconcile'](tmp_path, role, dry_run=True)
    assert snapshot(tmp_path) == before
    m['reconcile'](tmp_path, role)
    after = snapshot(tmp_path)
    assert not any(line.startswith('alias /') for line in installer_plan())
    assert snapshot(tmp_path) == after
    assert any(line.startswith('remove /') for line in installer_plan('--rollback-aliases'))
    assert snapshot(tmp_path) == after
    m['reconcile'](tmp_path, role)
    assert snapshot(tmp_path) == after
    for name, target in pairs:
        assert (tmp_path / name.lstrip('/')).samefile(tmp_path / target.lstrip('/'))
    assert (units / 'rynhult-broker@.service').samefile(units / 'acc-broker@.service')
    m['reconcile'](tmp_path, role, rollback=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('conflict', ['file', 'dangling', 'vendor', 'mask', 'dropin'])
def test_conflicts_preflight_all_before_mutation(tmp_path, conflict):
    (tmp_path / 'etc').mkdir(exist_ok=True)
    m = api()
    pairs = m['command_aliases']('main')
    for _, target in pairs:
        put(tmp_path, target)
    path = tmp_path / pairs[-1][0].lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    if conflict == 'file':
        path.write_text('operator file')
    elif conflict == 'dangling':
        path.symlink_to('/missing')
    else:
        put(tmp_path, '/etc/systemd/system/accd.service', '[Service]\n')
        if conflict == 'vendor':
            put(tmp_path, '/usr/lib/systemd/system/rynhultd.service', '[Service]\n')
        elif conflict == 'mask':
            unit = tmp_path / 'etc/systemd/system/accd.service'
            unit.unlink()
            unit.symlink_to('/dev/null')
        else:
            (tmp_path / 'etc/systemd/system/rynhultd.service.d').mkdir()
    before = snapshot(tmp_path)
    with pytest.raises(ValueError):
        m['reconcile'](tmp_path, 'main')
    assert snapshot(tmp_path) == before


def test_rollback_refuses_changed_alias_without_partial_removal(tmp_path):
    (tmp_path / 'etc').mkdir(exist_ok=True)
    m = api()
    for _, target in m['command_aliases']('main'):
        put(tmp_path, target)
    m['reconcile'](tmp_path, 'main')
    name = m['command_aliases']('main')[-1][0]
    path = tmp_path / name.lstrip('/')
    path.unlink()
    path.write_text('replacement')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError):
        m['reconcile'](tmp_path, 'main', rollback=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('role,installer', [('main', 'deploy/install.sh'), ('node', 'deploy/node/install.sh')])
def test_installer_alias_only_dryrun_is_unprivileged_and_readonly(tmp_path, role, installer):
    (tmp_path / 'etc').mkdir()
    put(tmp_path, '/usr/local/sbin/acc-agent-add')
    before = snapshot(tmp_path)
    result = subprocess.run(['bash', str(REPO / installer), '--aliases-only', '--dry-run',
                             '--alias-root', str(tmp_path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'rynhult-agent-add' in result.stdout
    assert snapshot(tmp_path) == before
    assert 'restart' not in result.stdout and 'enable --now' not in result.stdout


def test_console_scripts_share_implementation():
    import tomllib
    scripts = tomllib.loads((REPO / 'pyproject.toml').read_text())['project']['scripts']
    for old in ('acc', 'accd', 'acc-node', 'acc-guard', 'acc-restore', 'acc-db-transfer'):
        assert scripts['rynhult' + old[3:]] == scripts[old]


@pytest.mark.parametrize('kind', ['parent-link', 'writable-parent', 'target-link', 'journal-link', 'journal-tamper'])
def test_rejects_untrusted_paths_and_journal(tmp_path, kind):
    (tmp_path / 'etc').mkdir()
    m = api()
    source = put(tmp_path, '/usr/local/sbin/acc-agent-add')
    if kind == 'parent-link':
        source.parent.rename(source.parent.with_name('actual'))
        source.parent.symlink_to('actual')
    elif kind == 'writable-parent':
        source.parent.chmod(0o777)
    elif kind == 'target-link':
        source.unlink()
        source.symlink_to('/bin/sh')
    elif kind == 'journal-link':
        (tmp_path / 'etc/rynhult-phase-b.json').symlink_to('/etc/passwd')
    else:
        put(tmp_path, '/etc/rynhult-phase-b.json',
            '{"schema":1,"role":"main","links":{"/etc/passwd":"anything"}}')
    before = snapshot(tmp_path)
    with pytest.raises((OSError, ValueError)):
        m['reconcile'](tmp_path, 'main')
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('checkpoint', [0, 1, 3])
def test_resume_after_interrupted_alias_creation(tmp_path, monkeypatch, checkpoint):
    (tmp_path / 'etc').mkdir()
    m = api()
    for _, target in m['command_aliases']('main'):
        put(tmp_path, target)
    before = snapshot(tmp_path)
    mutate = m['Host'].mutate
    calls = 0

    def interrupted(self, path, target=None):
        nonlocal calls
        if calls == checkpoint:
            raise OSError('simulated power loss')
        calls += 1
        return mutate(self, path, target)

    monkeypatch.setattr(m['Host'], 'mutate', interrupted)
    with pytest.raises(OSError, match='power loss'):
        m['reconcile'](tmp_path, 'main')
    monkeypatch.setattr(m['Host'], 'mutate', mutate)
    m['reconcile'](tmp_path, 'main')
    m['reconcile'](tmp_path, 'main', rollback=True)
    m['reconcile'](tmp_path, 'main', rollback=True)
    assert snapshot(tmp_path) == before


def test_suite_does_not_inherit_live_session_routes():
    # Either spelling would redirect old-only subprocess fixture overrides.
    for suffix in ('RUN_DIR', 'SESSION_ID', 'GUARD_FILES'):
        assert 'ACC_' + suffix not in os.environ
        assert 'RYNHULT_' + suffix not in os.environ


def test_all_shipped_systemd_units_have_alias_coverage():
    names = set(api()['UNIT_API']['UNITS'])
    for path in (REPO / 'deploy').rglob('acc*'):
        if path.suffix in ('.service', '.socket', '.timer', '.path'):
            assert path.name in names, path


def test_vendor_instance_cannot_be_hidden_by_template_alias(tmp_path):
    units = tmp_path / 'etc/systemd/system'
    vendor = tmp_path / 'usr/lib/systemd/system'
    units.mkdir(parents=True)
    vendor.mkdir(parents=True)
    (units / 'acc-broker@.socket').write_text('[Socket]\n')
    (vendor / 'rynhult-broker@demo.socket').symlink_to('/dev/null')
    with pytest.raises(ValueError, match='conflict'):
        api()['reconcile'](tmp_path, 'node')


@pytest.mark.parametrize('state', ['[]', 'null', '"bad"'])
def test_corrupt_journal_fails_closed(tmp_path, state):
    put(tmp_path, '/etc/rynhult-phase-b.json', state)
    with pytest.raises(ValueError):
        api()['reconcile'](tmp_path, 'main')


@pytest.mark.parametrize('role', ['main', 'node'])
def test_alias_only_adds_core_commands_to_014_layout(tmp_path, role):
    (tmp_path / 'etc').mkdir()
    m = api()
    prefix = '/srv/acc' if role == 'main' else '/opt/acc'
    for old in ('acc', 'accd', 'acc-node'):
        put(tmp_path, f'{prefix}/app/.venv/bin/{old}')
    put(tmp_path, '/usr/local/bin/acc-node')
    m['reconcile'](tmp_path, role)
    for name in ('rynhult', 'rynhultd', 'rynhult-node'):
        assert (tmp_path / 'usr/local/bin' / name).is_file()


@pytest.mark.parametrize('role', ['main', 'node'])
def test_core_stage_one_entry_points_are_retained(tmp_path, role):
    (tmp_path / 'etc').mkdir()
    m = api()
    prefix = '/srv/acc' if role == 'main' else '/opt/acc'
    for old, new in [('acc', 'rynhult'), ('accd', 'rynhultd'), ('acc-node', 'rynhult-node')]:
        put(tmp_path, f'{prefix}/app/.venv/bin/{old}')
        put(tmp_path, f'{prefix}/app/.venv/bin/{new}')
        alias = tmp_path / f'usr/local/bin/{new}'
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(f'{prefix}/app/.venv/bin/{new}')
    put(tmp_path, '/usr/local/bin/acc-node')
    before = snapshot(tmp_path)
    m['reconcile'](tmp_path, role)
    m['reconcile'](tmp_path, role, rollback=True)
    assert snapshot(tmp_path) == before


def test_preexisting_stage_one_command_must_have_protected_target(tmp_path):
    (tmp_path / 'etc').mkdir()
    put(tmp_path, '/srv/acc/app/.venv/bin/acc')
    unsafe = put(tmp_path, '/srv/acc/app/.venv/bin/rynhult')
    unsafe.chmod(0o777)
    alias = tmp_path / 'usr/local/bin/rynhult'
    alias.parent.mkdir(parents=True)
    alias.symlink_to('/srv/acc/app/.venv/bin/rynhult')
    with pytest.raises(ValueError, match='untrusted'):
        api()['reconcile'](tmp_path, 'main')


def test_installers_journal_new_unit_aliases_before_enablement():
    for name in ('deploy/install.sh', 'deploy/node/install.sh', 'deploy/telemetry/install.sh'):
        for line in (REPO / name).read_text().splitlines():
            if 'unit-aliases.py' in line:
                assert '--check' in line, f'Unjournaled alias mutation in {name}: {line}'


def test_systemd_enable_preserves_journaled_alias_identity(tmp_path):
    import shutil
    systemctl = shutil.which('systemctl')
    if not systemctl:
        pytest.skip('systemctl not installed')
    units = tmp_path / 'etc/systemd/system'
    units.mkdir(parents=True)
    for name in ('accd.service', 'accd.socket'):
        shutil.copyfile(REPO / 'deploy' / name, units / name)
    m = api()
    m['reconcile'](tmp_path, 'main')
    result = subprocess.run([systemctl, '--root', str(tmp_path), 'enable', 'accd.socket'],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    m['reconcile'](tmp_path, 'main', rollback=True)
    assert not os.path.lexists(units / 'rynhultd.socket')
    assert (units / 'accd.socket').is_file()
    # The operator's enablement is outside B ownership and survives rollback.
    assert (units / 'sockets.target.wants/accd.socket').is_symlink()


def test_macos_command_aliases_have_user_owned_rollback_without_job_changes(tmp_path):
    m = api()
    prefix = '/Users/alex/Library/Application Support/ACC'
    public = '/opt/homebrew/bin'
    options = {'mac_prefix': prefix, 'mac_bin': public}
    for _, target in m['command_aliases']('macos', **options):
        put(tmp_path, target)
    (tmp_path / public.lstrip('/')).mkdir(parents=True, exist_ok=True)
    put(tmp_path, '/Users/alex/Library/LaunchAgents/local.acc.node.plist', 'unchanged job')
    before = snapshot(tmp_path)
    m['reconcile'](tmp_path, 'macos', dry_run=True, **options)
    assert snapshot(tmp_path) == before
    m['reconcile'](tmp_path, 'macos', **options)
    after = snapshot(tmp_path)
    m['reconcile'](tmp_path, 'macos', **options)
    assert snapshot(tmp_path) == after
    assert (tmp_path / 'opt/homebrew/bin/rynhult-request').is_file()
    m['reconcile'](tmp_path, 'macos', rollback=True, **options)
    assert snapshot(tmp_path) == before


def test_legacy_vendor_template_dropins_remain_authoritative(tmp_path):
    put(tmp_path, '/etc/systemd/system/acc-broker@.service', '[Service]\nKillMode=process\n')
    dropin = put(tmp_path, '/usr/lib/systemd/system/acc-broker@.service.d/limits.conf',
                 '[Service]\nTasksMax=64\n')
    original = dropin.read_bytes()
    api()['reconcile'](tmp_path, 'node')
    assert dropin.read_bytes() == original
    assert (tmp_path / 'etc/systemd/system/rynhult-broker@.service').readlink() == Path('acc-broker@.service')


def test_uninstaller_can_rollback_the_journaled_host_role(tmp_path):
    (tmp_path / 'etc').mkdir()
    put(tmp_path, '/usr/local/sbin/acc-agent-add')
    m = api()
    m['reconcile'](tmp_path, 'node')
    before = snapshot(tmp_path)
    result = subprocess.run(['/usr/bin/python3', '-I', str(SCRIPT), '--role', 'auto',
                             '--rollback-aliases', '--dry-run', '--alias-root', str(tmp_path)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'remove /usr/local/sbin/rynhult-agent-add' in result.stdout
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('role,unit', [('node', 'accd.service'), ('main', 'acc-node.service')])
def test_alias_only_preflight_rejects_the_wrong_host_role(tmp_path, role, unit):
    put(tmp_path, f'/etc/systemd/system/{unit}', '[Service]\n')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match='host role'):
        api()['reconcile'](tmp_path, role)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('arguments', [['--alias-root', '/offline'], ['--aliases-onl'], ['--dryrun']])
def test_main_unknown_options_never_fall_through_to_a_real_install(arguments):
    result = subprocess.run(['bash', str(REPO / 'deploy/install.sh'), *arguments],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert 'unknown option' in result.stderr
    assert result.stdout == ''


@pytest.mark.parametrize('installer', ['deploy/install.sh', 'deploy/node/install.sh'])
def test_alias_only_wrapper_never_loads_a_path_supplied_dirname(tmp_path, installer):
    root = tmp_path / 'host'
    (root / 'etc').mkdir(parents=True)
    tools = tmp_path / 'tools'
    tools.mkdir()
    marker = tmp_path / 'executed'
    fake = tools / 'dirname'
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\nexec /usr/bin/dirname "$@"\n')
    fake.chmod(0o755)
    result = subprocess.run(['bash', str(REPO / installer), '--aliases-only', '--dry-run',
                             '--alias-root', str(root)], env={'PATH': f'{tools}:/usr/bin:/bin'},
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), 'alias-only dispatch executed a caller-supplied program'


def test_mac_alias_dispatch_rejects_root_before_homebrew_lookup(tmp_path):
    header = (REPO / 'deploy/node/macos/install.sh').read_text().split('acc_compat_lib=', 1)[0]
    # Simulate root only in the dispatch fragment; never run the real installer as root.
    tools = tmp_path / 'tools'
    tools.mkdir()
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\necho 0\n')
    identity.chmod(0o755)
    marker = tmp_path / 'brew-called'
    brew = tools / 'brew'
    brew.write_text(f'#!/bin/sh\ntouch "{marker}"\necho "{tmp_path}"\n')
    brew.chmod(0o755)
    result = subprocess.run(['bash', '-eu', '-c', header, 'fixture', '--aliases-only', '--dry-run'],
                            env={'PATH': f'{tools}:/usr/bin:/bin', 'HOME': str(tmp_path)},
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert 'not root' in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize('role', ['main', 'node', 'macos'])
@pytest.mark.parametrize('suffix', ['hook', 'statusline'])
@pytest.mark.parametrize('env_prefix', ['ACC_', 'RYNHULT_'])
def test_installed_shell_alias_uses_its_bridge_and_only_the_fixture_spool(tmp_path, role, suffix, env_prefix):
    import json
    import shutil
    (tmp_path / 'etc').mkdir()
    options = {}
    if role == 'macos':
        prefix, public = '/Users/alex/Library/Application Support/ACC', '/opt/homebrew/bin'
        options = {'mac_prefix': prefix, 'mac_bin': public}
    else:
        prefix = '/srv/acc' if role == 'main' else '/opt/acc'
        public = '/usr/local/bin'
    source = put(tmp_path, f'{prefix}/bin/acc-{suffix}')
    shutil.copyfile(REPO / f'bin/acc-{suffix}', source)
    for folder in (f'{prefix}/bin', public):
        bridge = put(tmp_path, f'{folder}/acc-env.sh')
        shutil.copyfile(REPO / 'deploy/lib/acc-env.sh', bridge)
    api()['reconcile'](tmp_path, role, **options)
    run = tmp_path / 'fixture-run'
    run.mkdir()
    command = tmp_path / public.lstrip('/') / f'rynhult-{suffix}'
    result = subprocess.run([str(command), 'claude', 'Stop'], input='{"hook_event_name":"Stop"}',
                            text=True, capture_output=True,
                            env={'PATH': '/usr/bin:/bin', env_prefix + 'RUN_DIR': str(run),
                                 env_prefix + 'SESSION_ID': 'fixture'})
    assert result.returncode == 0, result.stderr
    if suffix == 'hook':
        record = json.loads((run / 'events.jsonl').read_text())
        assert record['acc_session'] == 'fixture' and record['event'] == 'Stop'
    else:
        assert json.loads((run / 'statusline.json').read_text()) == {'hook_event_name': 'Stop'}
        assert result.stdout.startswith('Rynhult fixture')


@pytest.mark.parametrize('role,name', [('main', 'accd.service'), ('node', 'acc-broker@.socket')])
def test_offline_update_preserves_host_absolute_unit_aliases(tmp_path, role, name):
    put(tmp_path, f'/etc/systemd/system/{name}', '[Unit]\n')
    directory = tmp_path / 'etc/systemd/system'
    target = '/etc/systemd/system/' + name
    (directory / ('rynhult' + name[3:])).symlink_to(target)
    if '@.' in name:
        (directory / 'rynhult-broker@alex.socket').symlink_to(target)
    before = snapshot(tmp_path)
    m = api()
    m['reconcile'](tmp_path, role, dry_run=True)
    m['reconcile'](tmp_path, role)
    m['reconcile'](tmp_path, role, rollback=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('uid', [0, 501])
@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('rollback', [False, True])
def test_macos_cli_privilege_guard_allows_readonly_root_inspection(monkeypatch, uid, dry_run, rollback):
    import sys
    main = api()['main']
    calls = []
    monkeypatch.setattr(os, 'geteuid', lambda: uid)
    monkeypatch.setitem(main.__globals__, 'reconcile', lambda *a, **kw: calls.append((a, kw)))
    arguments = ['public-aliases.py', '--role', 'macos', '--mac-prefix', '/fixture',
                 '--mac-bin', '/fixture/bin']
    if dry_run:
        arguments.append('--dry-run')
    if rollback:
        arguments.append('--rollback-aliases')
    monkeypatch.setattr(sys, 'argv', arguments)
    if uid == 0 and not dry_run:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
        assert calls == []
    else:
        main()
        assert len(calls) == 1
        assert calls[0][1]['dry_run'] is dry_run
        assert calls[0][1]['rollback'] is rollback


@pytest.mark.parametrize(('platform', 'user_mode', 'mode', 'owner', 'group', 'allowed'), [
    ('darwin', True, 0o775, 501, 1234, True),
    ('darwin', True, 0o777, 501, 1234, False),
    ('darwin', True, 0o775, 501, 20, False),
    ('darwin', True, 0o775, 502, 1234, False),
    ('darwin', True, 0o775, 0, 1234, False),
    ('darwin', True, 0o1777, 501, 1234, False),
    ('darwin', False, 0o775, 501, 1234, False),
    ('linux', True, 0o775, 501, 1234, False),
    ('linux', False, 0o775, 501, 1234, False),
    ('darwin', True, 0o755, 501, 20, True),
])
def test_homebrew_directory_trust_is_macos_user_admin_only(tmp_path, monkeypatch, platform, user_mode,
                                                         mode, owner, group, allowed):
    import grp
    import stat
    from types import SimpleNamespace

    m = api()
    monkeypatch.setattr(m['sys'], 'platform', platform)
    monkeypatch.setattr(grp, 'getgrnam', lambda name: SimpleNamespace(gr_gid=1234) if name == 'admin' else None)
    host = m['Host'](tmp_path, user_mode=user_mode)
    try:
        monkeypatch.setattr(m['os'], 'geteuid', lambda: 501)
        info = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=owner, st_gid=group)
        if allowed:
            host.check(info)
        else:
            with pytest.raises(ValueError, match='untrusted alias path'):
                host.check(info)
        # Writable files never inherit the directory exception, even under admin.
        if mode & 0o022:
            with pytest.raises(ValueError, match='untrusted alias path'):
                host.check(SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner, st_gid=group))
    finally:
        os.close(host.fd)


def test_missing_macos_admin_group_keeps_strict_directory_permissions(tmp_path, monkeypatch):
    import grp
    import stat
    from types import SimpleNamespace

    m = api()
    monkeypatch.setattr(m['sys'], 'platform', 'darwin')

    def missing(name):
        assert name == 'admin'
        raise KeyError(name)

    monkeypatch.setattr(grp, 'getgrnam', missing)
    host = m['Host'](tmp_path, user_mode=True)
    try:
        with pytest.raises(ValueError, match='untrusted alias path'):
            host.check(SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=os.geteuid(), st_gid=80))
    finally:
        os.close(host.fd)


@pytest.mark.parametrize('absolute', [False, True])
def test_merged_local_sbin_supports_preflight_install_and_rollback(tmp_path, absolute):
    (tmp_path / 'etc').mkdir()
    (tmp_path / 'usr/local/bin').mkdir(parents=True)
    (tmp_path / 'usr/local/sbin').symlink_to('/usr/local/bin' if absolute else 'bin')
    put(tmp_path, '/usr/local/bin/acc-agent-add')
    before = snapshot(tmp_path)
    m = api()
    m['reconcile'](tmp_path, 'main', dry_run=True)
    assert snapshot(tmp_path) == before
    m['reconcile'](tmp_path, 'main')
    link = tmp_path / 'usr/local/bin/rynhult-agent-add'
    assert link.is_symlink() and os.readlink(link) == 'acc-agent-add'
    m['reconcile'](tmp_path, 'main')
    m['reconcile'](tmp_path, 'main', rollback=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('logical,target', [('/usr/sbin', 'bin'), ('/sbin', 'usr/bin'),
                                           ('/sbin', 'usr/sbin'), ('/bin', 'usr/bin')])
def test_standard_merged_bin_parents_remain_inside_offline_root(tmp_path, logical, target):
    original = put(tmp_path, '/usr/bin/acc-demo')
    link = tmp_path / logical.lstrip('/')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    if target == 'usr/sbin':
        (tmp_path / 'usr/sbin').symlink_to('bin')
    host = api()['Host'](tmp_path)
    try:
        assert host.info(logical + '/acc-demo').st_ino == original.stat().st_ino
        host.mutate(logical + '/rynhult-demo', 'acc-demo')
        assert os.readlink(tmp_path / 'usr/bin/rynhult-demo') == 'acc-demo'
        host.mutate(logical + '/rynhult-demo')
        assert not (tmp_path / 'usr/bin/rynhult-demo').exists()
    finally:
        os.close(host.fd)


def test_merged_parent_does_not_allow_writable_canonical_directory(tmp_path):
    (tmp_path / 'etc').mkdir()
    original = put(tmp_path, '/usr/local/bin/acc-agent-add')
    (tmp_path / 'usr/local/sbin').symlink_to('bin')
    original.parent.chmod(0o777)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match='untrusted'):
        api()['reconcile'](tmp_path, 'main')
    assert snapshot(tmp_path) == before
