"""The owner kit is exercised with a private filesystem and fake commands, never root."""
import importlib.util
import json
import os
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('containers_qualification_kit', ROOT / 'scripts/containers-qualification/kit.py')


@pytest.fixture
def kit():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


@pytest.fixture
def host(kit, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('test attempted an unstubbed host command')
    monkeypatch.setattr(kit.subprocess, 'run', forbidden)
    host = kit.Host(root=tmp_path, owner=os.getuid())
    host.path('/var/lib/acc').mkdir(parents=True)
    return host


@pytest.mark.parametrize('text', [
    'alex-agent:100000:65536\nother:120000:65536\n',
    'alex-agent:100000:65535\n', 'alex-agent:0:65536\n',
    'alex-agent:not-a-number:65536\n', 'other:100000:65536\n',
])
def test_subordinate_ids_refuse_missing_small_overlapping_or_invalid_ranges(kit, text):
    with pytest.raises(ValueError):
        kit.subordinate_ranges(text, 'alex-agent', 1001)


def test_numeric_subordinate_id_alias_and_comments(kit):
    assert kit.subordinate_ranges('# comment\n1001:100000:65536\nother:200000:65536\n', 'alex-agent', 1001) == [(100000, 65536)]


def test_journal_refuses_rerun_and_restores_exact_file_bytes_and_mode(kit, host):
    path = host.path('/policy.json')
    path.write_bytes(b'{"images": {}}\n')
    path.chmod(0o640)
    journal = kit.Journal.create(host)
    journal.write('/policy.json', b'new\n', 0o644)
    with pytest.raises(FileExistsError):
        kit.Journal.create(host)
    journal.finish('ready')
    journal.restore_files()
    assert path.read_bytes() == b'{"images": {}}\n'
    assert path.stat().st_mode & 0o777 == 0o640


def test_uninstall_prevalidates_all_files_before_restoring_any(kit, host):
    journal = kit.Journal.create(host)
    journal.write('/first', b'ours', 0o644)
    journal.write('/second', b'ours', 0o644)
    host.path('/second').write_bytes(b'changed by owner')
    with pytest.raises(ValueError, match='changed'):
        journal.restore_files()
    assert host.path('/first').read_bytes() == b'ours'
    assert host.path('/second').read_bytes() == b'changed by owner'


def test_replaced_file_symlink_and_unowned_tree_entries_are_never_deleted(kit, host):
    journal = kit.Journal.create(host)
    journal.write('/created', b'ours', 0o644)
    host.path('/created').unlink()
    host.path('/created').symlink_to(host.path('/victim'))
    with pytest.raises(ValueError):
        journal.restore_files()
    assert host.path('/created').is_symlink()
    directory = journal.mkdir('/owned')
    (directory / 'ours').write_text('one')
    journal.record_tree('/owned')
    (directory / 'not-ours').write_text('two')
    with pytest.raises(ValueError, match='changed'):
        journal.validate_tree('/owned')
    assert (directory / 'not-ours').read_text() == 'two'


def test_pending_write_is_not_blindly_removed_after_interruption(kit, host):
    journal = kit.Journal.create(host)
    journal.data['pending'] = '/unknown'
    journal.save()
    with pytest.raises(ValueError, match='pending'):
        journal.restore_files()


def test_command_failure_stops_and_uses_clean_environment(kit, host, monkeypatch):
    executable = host.path('/usr/bin/false')
    executable.parent.mkdir(parents=True)
    executable.write_text('fake executable; never launched')
    executable.chmod(0o755)
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        raise kit.subprocess.CalledProcessError(17, args, stderr='failed')
    monkeypatch.setattr(kit.subprocess, 'run', run)
    with pytest.raises(kit.subprocess.CalledProcessError):
        host.run(['/usr/bin/false'])
    assert len(seen) == 1
    assert seen[0][1]['check'] is True
    assert seen[0][1]['env'] == {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}


@pytest.mark.parametrize('name', ['preflight', 'setup', 'collect', 'uninstall'])
def test_shell_entrypoints_fail_closed_and_isolate_python(name):
    text = (ROOT / f'scripts/containers-qualification/{name}.sh').read_text()
    assert 'set -euo pipefail' in text
    assert '/usr/bin/python3 -I' in text
    assert 'exec ' in text
    assert 'sudo' not in text


def test_manifest_is_a_reviewable_allowlist():
    plan = json.loads((ROOT / 'scripts/containers-qualification/manifest.json').read_text())
    assert plan['version'] == 1
    assert plan['enable_containers'] is False
    assert plan['install_packages'] is False
    assert plan['delete_existing_images'] is False
    assert set(plan['commands']) == {'preflight', 'setup', 'collect', 'uninstall'}


@pytest.fixture
def prepared(kit, host, monkeypatch):
    """A simulated installed host. Every subprocess is replaced, including Podman."""
    from types import SimpleNamespace
    source = host.path('/reviewed')
    source.mkdir()
    for name in ['deploy/apparmor/acc-podman', 'deploy/apparmor/acc-podman-runtime',
                 'deploy/containers/storage.conf']:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(kit, 'SOURCE', source)
    monkeypatch.setattr(kit, 'review_hashes', lambda: {'reviewed-source': 'a' * 64})
    monkeypatch.setattr(kit, 'source_protected', lambda h: None)
    account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir='/home/alex-agent')
    monkeypatch.setattr(kit.pwd, 'getpwnam', lambda name: account)
    plan = {'account': 'alex-agent', 'archive': '/archive', 'archive_sha256': 'a' * 64,
            'image': 'sha256:' + 'b' * 64, 'podman_version': '4.9.3', 'crun_version': '1.14.4',
            'repo': '/work/demo', 'root': '/work'}
    for name in ['/etc/apparmor.d', '/etc/sysctl.d', '/etc/acc/containers', '/run', '/srv/acc/containers']:
        host.path(name).mkdir(parents=True, exist_ok=True)
    original_policy = b'{"version":1,"images":{"default":{"image":"old","custom":true},"other":{"keep":true}}}\n'
    host.path(kit.POLICY).write_bytes(original_policy)
    host.path(kit.RESTRICT).parent.mkdir(parents=True)
    host.path(kit.RESTRICT).write_text('0\n')
    profiles = host.path('/sys/kernel/security/apparmor/profiles')
    profiles.parent.mkdir(parents=True)
    profiles.write_text('')
    broker = host.path('/proc/999')
    broker.mkdir(parents=True)
    (broker / 'status').write_text(f'Uid: {account.pw_uid} {account.pw_uid} {account.pw_uid} {account.pw_uid}\n')
    (broker / 'cgroup').write_text('0::/system.slice/acc-broker@alex-agent.service\n')
    (broker / 'cmdline').write_bytes(b'/srv/acc/app/.venv/bin/python\0-I\0-m\0acc.broker.server\0')
    calls, state = [], {'linger': 'no', 'fail': None,
                        'exec': '{ path=/srv/acc/app/.venv/bin/python ; argv[]=/srv/acc/app/.venv/bin/python -I -m '
                                'acc.broker.server ; ignore_errors=no ; start_time=[n/a] ; pid=0 ; code=(null) }'}
    def run(args, **kwargs):
        calls.append(args)
        if state['fail'] and state['fail'] in ' '.join(args):
            raise kit.subprocess.CalledProcessError(17, args, output='', stderr='injected failure')
        if 'list-units' in args:
            return ''
        if 'MainPID' in args:
            return '999'
        if 'ExecStart' in args:
            return state['exec']
        if 'show-user' in args:
            return state['linger']
        if 'enable-linger' in args:
            state['linger'] = 'yes'
        if 'disable-linger' in args:
            state['linger'] = 'no'
        if args[0] == '/usr/sbin/sysctl':
            host.path(kit.RESTRICT).write_text(args[-1].rsplit('=', 1)[1])
        if args[0] == '/usr/sbin/apparmor_parser':
            if '-a' in args:
                profiles.write_text(profiles.read_text() + '\n'.join(
                    name + ' (unconfined)' for name in kit.re.findall(r'^profile ([^ ]+)', Path(args[-1]).read_text(), kit.re.M)))
            else:
                profiles.write_text('')
        return 'active'
    monkeypatch.setattr(host, 'run', run)
    original_preflight = kit.preflight
    monkeypatch.setattr(kit, 'preflight', lambda h, p, **kwargs: {'status': 'pass'})
    def publish(journal, plan):
        for name in (kit.STORE, kit.RUNROOT):
            directory = journal.mkdir(name)
            (directory / 'created').write_text('owned')
            journal.record_tree(name)
    original_publish = kit.publish_image
    monkeypatch.setattr(kit, 'publish_image', publish)
    return SimpleNamespace(host=host, plan=plan, calls=calls, state=state, original_policy=original_policy, original_preflight=original_preflight, original_publish=original_publish)


def test_setup_and_uninstall_preserve_unrelated_policy_and_restore_settings(kit, prepared):
    p = prepared
    journal = kit.setup(p.host, p.plan)
    policy = json.loads(p.host.path(kit.POLICY).read_text())
    assert policy['images']['default']['custom'] is True
    assert policy['images']['other'] == {'keep': True}
    assert policy['images']['qualification-phase1']['network'] == 'none'
    assert p.state['linger'] == 'yes'
    assert p.host.path(kit.RESTRICT).read_text() == '1'
    kit.uninstall(journal)
    assert p.host.path(kit.POLICY).read_bytes() == p.original_policy
    assert p.state['linger'] == 'no'
    assert p.host.path(kit.RESTRICT).read_text() == '0'
    assert not p.host.path(kit.STORE).exists()
    assert journal.data['status'] == 'uninstalled'
    with pytest.raises(FileExistsError):
        kit.setup(p.host, p.plan)


def test_setup_failure_stops_before_image_or_policy_and_is_audited(kit, prepared):
    p = prepared
    p.state['fail'] = 'kernel.apparmor_restrict_unprivileged_userns=1'
    with pytest.raises(kit.subprocess.CalledProcessError):
        kit.setup(p.host, p.plan)
    journal = kit.Journal.load(p.host)
    assert journal.data['status'] == 'failed'
    assert journal.data['settings']['userns']['pending'] is True
    assert p.host.path(kit.POLICY).read_bytes() == p.original_policy
    assert not p.host.path(kit.STORE).exists()
    assert not any('podman' in command[0] for command in p.calls)
    with pytest.raises(ValueError, match='pending'):
        kit.uninstall(journal)


def test_uninstall_removes_only_its_collector_files_in_its_created_directory(kit, prepared):
    journal = kit.setup(prepared.host, prepared.plan)
    proof = '/etc/acc/containers/qualification/alex-agent.json'
    journal.write(proof, b'{"status":"pass"}')
    journal.record_tree('/etc/acc/containers/qualification')
    kit.uninstall(journal)
    assert not prepared.host.path(proof).exists()


def test_existing_store_or_partial_distribution_profiles_are_not_repaired(kit, prepared):
    p = prepared
    p.host.path(kit.STORE).mkdir()
    with pytest.raises(FileExistsError):
        kit.setup(p.host, p.plan)
    assert not p.host.path(kit.STATE).exists()
    p.host.path(kit.STORE).rmdir()
    p.host.path('/etc/apparmor.d/podman').write_text('existing')
    with pytest.raises(ValueError, match='only one'):
        kit.setup(p.host, p.plan)
    assert not p.host.path(kit.STATE).exists()


def test_collector_failure_writes_no_go_json_and_refuses_a_second_run(kit, prepared, monkeypatch):
    journal = kit.setup(prepared.host, prepared.plan)
    def blocked(*args, **kwargs):
        return {'status': 'blocked', 'checks': [{'name': 'image', 'status': 'missing'}]}
    monkeypatch.setattr(kit, 'preflight', blocked)
    assert kit.collect(journal) == 1
    report = json.loads((journal.folder / 'report.json').read_text())
    assert report['go_phase_1'] is False
    assert report['status'] == 'no-go'
    assert 'preflight' in report['error']
    with pytest.raises(ValueError, match='once'):
        kit.collect(journal)
    assert not any('systemd-run' in command[0] for command in prepared.calls)


def test_a_passing_preflight_row_never_carries_the_failure_text(kit, prepared):
    """Attempt 17's report: every passing row built with expect() showed its failure wording ("missing executable:
    /usr/bin/cp", "installed helper differs from reviewed source", "AppArmor must be enabled") under status pass."""
    report = prepared.original_preflight(prepared.host, prepared.plan)
    passed = [row for row in report['checks'] if row['status'] == 'pass']
    assert passed
    failure = re.compile(r'missing|differs|mismatch|must|required|unsupported|enable linger')
    assert [row for row in passed if failure.search(row['detail'])] == []
    assert all(row['detail'] for row in passed)


def test_mutating_entrypoints_refuse_unprivileged_context(kit, monkeypatch, capsys):
    monkeypatch.setattr(kit.os, 'geteuid', lambda: 1001)
    for command in ['setup', 'collect', 'uninstall']:
        monkeypatch.setattr(kit.sys, 'argv', ['kit.py', command])
        assert kit.main() == 2
    assert 'nothing changed' in capsys.readouterr().err


def test_preflight_collects_missing_prerequisites_without_writing(kit, prepared):
    p = prepared
    before = {str(path.relative_to(p.host.root)): path.read_bytes()
              for path in p.host.root.rglob('*') if path.is_file()}
    report = p.original_preflight(p.host, p.plan)
    names = {row['name'] for row in report['checks']}
    assert {'linger', 'subuid', 'subgid', 'AppArmor enabled', 'AppArmor profiles',
            'restricted user namespaces', 'user-manager delegation and bus',
            'cgroup v2 controllers', 'podman version', 'crun version', 'image execution store',
            'production containers disabled', 'control account and grants'} <= names
    assert report['status'] == 'blocked'
    assert all(row['detail'] for row in report['checks'])
    assert before == {str(path.relative_to(p.host.root)): path.read_bytes()
                      for path in p.host.root.rglob('*') if path.is_file()}
    assert not any(any(word in command for word in ('enable-linger', '-w', 'start', 'load', 'run'))
                   for command in p.calls)


def test_loaded_profile_names_are_exact_not_substring_matches(kit, host):
    path = host.path('/sys/kernel/security/apparmor/profiles')
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join('evil-' + name + ' (unconfined)' for name in
                             ['acc-newuidmap', 'acc-newgidmap', 'acc-pasta', 'podman', 'crun']))
    with pytest.raises(ValueError, match='missing loaded'):
        kit.profiles_ready(host)


@pytest.mark.parametrize('value', ['1', 'true', 'yes', '$(anything)', '${ACC_FLAG}'])
def test_feature_flag_preflight_refuses_enabled_or_indirect_values_without_executing(kit, host, value):
    path = host.path('/etc/default/accd')
    path.parent.mkdir(parents=True)
    path.write_text('ACC_CONTAINERS=' + value + '\n')
    with pytest.raises(ValueError, match='must be disabled'):
        kit.containers_disabled(host)


def test_feature_flag_checks_running_process_without_reporting_other_environment(kit, host, monkeypatch):
    path = host.path('/proc/4321/environ')
    path.parent.mkdir(parents=True)
    path.write_bytes(b'SECRET=must-not-be-reported\0ACC_CONTAINERS=1\0')
    monkeypatch.setattr(host, 'run', lambda args: '4321')
    with pytest.raises(ValueError, match='running accd') as error:
        kit.containers_disabled(host)
    assert 'must-not-be-reported' not in str(error.value)


@pytest.fixture
def collector(kit, prepared, monkeypatch):
    p = prepared
    journal = kit.setup(p.host, p.plan)
    proc = p.host.path('/proc/4321')
    proc.mkdir(parents=True)
    (proc / 'comm').write_text('sleep\n')
    # A complete /proc entry owned by a UID that is never the account's: under root (the gate queue) the
    # account is UID 0, so a hard-coded root UID would make it the account's own process.
    foreign = os.getuid() + 1
    (proc / 'status').write_text(f'Uid: {foreign} {foreign} {foreign} {foreign}\n')
    original_run = p.host.run
    state = {'proof_failure': False, 'phase1_pass': True, 'cleanup_failure': False}
    import ast
    tree = ast.parse((ROOT / 'scripts/container_qualification/phase1.py').read_text())
    mandatory = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MANDATORY' for t in node.targets))
    def run(args, **kwargs):
        if 'MainPID' in args and not any(arg.startswith('acc-broker@') for arg in args):
            p.calls.append(args)
            return '4321'
        if 'stop' in args and state['cleanup_failure']:
            p.calls.append(args)
            raise kit.subprocess.CalledProcessError(1, args)
        if args[0] == '/usr/sbin/runuser':
            p.calls.append(args)
            if any('image_store.py' in arg for arg in args):
                if state['proof_failure']:
                    state['proof_stderr_limit'] = kwargs.get('stderr_limit')
                    raise kit.subprocess.CalledProcessError(1, args, output='', stderr=state.get('proof_stderr', ''))
                state['proof_limit'] = kwargs.get('limit')
                state['proof_stderr_limit'] = kwargs.get('stderr_limit')
                return json.dumps({'account': p.plan['account'], 'image': p.plan['image'], 'time': 1.0,
                                   'status': 'pass', 'direct_write': 'denied', 'unshare_write': 'denied',
                                   'runtime_layers': 'protected', 'execution_source': 'held-rootfs',
                                   'namespace_boundary': 'denied', 'files': 3, 'note': 'agent-chosen text'})
            checks = {name: {'status': 'pass', 'reason': 'test'} for name in mandatory}
            if not state['phase1_pass']:
                checks['trusted_launcher']['status'] = 'skip'
            return json.dumps({'go_phase_1': True, 'checks': checks})
        return original_run(args, **kwargs)
    monkeypatch.setattr(p.host, 'run', run)
    return journal, state


def test_collector_passes_only_with_all_mandatory_rows_and_uninstall_preserves_report(kit, collector):
    journal, state = collector
    assert kit.collect(journal) == 0
    report_path = journal.folder / 'report.json'
    report = json.loads(report_path.read_text())
    assert report['go_phase_1'] is True and report['production_qualified'] is False
    assert 'probe_unit' not in journal.data
    kit.uninstall(journal)
    assert report_path.read_text()


@pytest.mark.parametrize('failure', ['proof_failure', 'phase1_pass', 'cleanup_failure'])
def test_collector_stops_on_probe_cleanup_or_mandatory_failure(kit, prepared, collector, failure):
    journal, state = collector
    state[failure] = failure != 'phase1_pass'
    assert kit.collect(journal) == 1
    report = json.loads((journal.folder / 'report.json').read_text())
    assert report['go_phase_1'] is False
    if failure != 'phase1_pass':
        assert not any('--phase-1-plan' in args for args in prepared.calls)
    if failure == 'cleanup_failure':
        assert journal.data.get('probe_unit')
        with pytest.raises(ValueError, match='cleanup incomplete'):
            kit.uninstall(journal)
    else:
        assert 'probe_unit' not in journal.data


def test_archive_import_failure_stops_and_records_only_new_trees(kit, prepared, monkeypatch):
    p = prepared
    journal = kit.Journal.create(p.host)
    p.host.path(p.plan['archive']).write_bytes(b'fake archive')
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'load' in args:
            raise kit.subprocess.CalledProcessError(1, args)
        return p.plan['image']
    monkeypatch.setattr(p.host, 'run', run)
    with pytest.raises(kit.subprocess.CalledProcessError):
        p.original_publish(journal, p.plan)
    assert not any('mount' in args or 'inspect' in args or 'run' in args for args in calls)
    assert calls[0][0:2] == ['/usr/bin/python3', '-I']
    assert any('verify_image_archive.py' in arg for arg in calls[0])
    for name in (kit.STORE, kit.RUNROOT):
        journal.validate_tree(name)


def test_failed_archive_copy_still_allows_owned_tree_validation(kit, prepared, monkeypatch):
    p = prepared
    journal = kit.Journal.create(p.host)
    p.host.path(p.plan['archive']).write_bytes(b'fake archive')
    def full(*args):
        raise OSError('simulated disk full')
    monkeypatch.setattr(kit.shutil, 'copyfileobj', full)
    with pytest.raises(OSError, match='disk full'):
        p.original_publish(journal, p.plan)
    for name in (kit.STORE, kit.RUNROOT):
        journal.validate_tree(name)
    assert p.calls == []


def test_uninstall_refuses_unknown_tree_entries_before_any_rollback(kit, prepared):
    p = prepared
    journal = kit.setup(p.host, p.plan)
    policy = p.host.path(kit.POLICY).read_bytes()
    (p.host.path(kit.STORE) / 'owner-added').write_text('preserve')
    calls = len(p.calls)
    with pytest.raises(ValueError, match='changed'):
        kit.uninstall(journal)
    assert p.host.path(kit.POLICY).read_bytes() == policy
    assert p.state['linger'] == 'yes'
    assert all('apparmor_parser' not in args[0] and 'disable-linger' not in args for args in p.calls[calls:])


def test_new_file_publication_never_replaces_a_racing_file(kit, host, monkeypatch):
    journal = kit.Journal.create(host)
    original_atomic = kit.atomic
    def race(path, data, mode, *args, **kwargs):
        if path.name == 'new':
            path.write_text('someone else created this')
        return original_atomic(path, data, mode, *args, **kwargs)
    monkeypatch.setattr(kit, 'atomic', race)
    with pytest.raises(FileExistsError):
        journal.write('/new', b'ours')
    assert host.path('/new').read_text() == 'someone else created this'


def test_file_rollback_restores_extended_attributes_and_timestamp(kit, host):
    path = host.path('/original')
    path.write_bytes(b'original')
    os.setxattr(path, 'user.owner-note', b'retain this')
    os.utime(path, ns=(1700000000000000000, 1700000000000000000))
    journal = kit.Journal.create(host)
    journal.write('/original', b'changed')
    journal.restore_files()
    assert os.getxattr(path, 'user.owner-note') == b'retain this'
    assert path.stat().st_mtime_ns == 1700000000000000000


def test_namespace_creation_failure_retains_exact_attempted_unit_and_does_not_stop_unknown_unit(kit, prepared, collector, monkeypatch):
    journal, state = collector
    original = prepared.host.run
    def fail(args, **kwargs):
        if args[0] == '/usr/bin/systemd-run':
            prepared.calls.append(args)
            raise kit.subprocess.TimeoutExpired(args, 120)
        return original(args, **kwargs)
    monkeypatch.setattr(prepared.host, 'run', fail)
    assert kit.collect(journal) == 1
    assert journal.data.get('probe_unit', '').startswith('acc-cq-')
    assert not any('stop' in args or '--phase-1-plan' in args for args in prepared.calls)
    with pytest.raises(ValueError, match='cleanup incomplete'):
        kit.uninstall(journal)


def test_setting_does_not_accept_a_successful_command_without_state_change(kit, host, monkeypatch):
    journal = kit.Journal.create(host)
    path = host.path(kit.RESTRICT)
    path.parent.mkdir(parents=True)
    path.write_text('0')
    monkeypatch.setattr(host, 'run', lambda args: '')
    with pytest.raises(ValueError, match='did not take effect'):
        kit.setting(journal, 'userns', '0', '1', ['/usr/sbin/sysctl', '-w', 'test=1'])
    assert journal.data['settings']['userns']['pending'] is True


def test_image_preflight_refuses_a_writable_execution_file(kit, prepared):
    p = prepared
    root = p.host.path(kit.STORE) / 'rootfs' / p.plan['image'][7:]
    root.mkdir(parents=True)
    (root / '.acc-image-id').write_text(p.plan['image'] + '\n')
    binary = root / 'payload'
    binary.write_text('image data')
    binary.chmod(0o666)
    p.host.path('/srv/acc/containers/storage.conf').write_bytes((kit.SOURCE / 'deploy/containers/storage.conf').read_bytes())
    with pytest.raises(ValueError, match='unsafe image entry'):
        kit.store_ready(p.host, p.plan)


def test_preflight_checks_no_new_privs_of_its_execution_context(kit, prepared):
    report = prepared.original_preflight(prepared.host, prepared.plan)
    assert any(row['name'] == 'caller NoNewPrivs' and row['status'] == 'missing' for row in report['checks'])


def test_feature_flag_preflight_refuses_whitespace_around_enabled_assignment(kit, host):
    path = host.path('/etc/default/accd')
    path.parent.mkdir(parents=True)
    path.write_text('ACC_CONTAINERS = yes\n')
    with pytest.raises(ValueError, match='must be disabled'):
        kit.containers_disabled(host)


def test_administrative_agent_group_is_reported_before_qualification(kit, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(kit.grp, 'getgrall', lambda: [SimpleNamespace(gr_name='docker', gr_mem=['alex-agent'])])
    monkeypatch.setattr(kit.grp, 'getgrgid', lambda gid: SimpleNamespace(gr_name='alex-agent'))
    with pytest.raises(ValueError, match='administrative group'):
        kit.account_groups('alex-agent', 1001)


def test_publish_uses_verified_independent_rootfs_and_never_executes_image_as_root(kit, prepared, monkeypatch):
    p = prepared
    journal = kit.Journal.create(p.host)
    p.host.path(p.plan['archive']).write_bytes(b'fake archive')
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'mount' in args:
            directory = p.host.path(kit.STORE) / 'mounted'
            directory.mkdir()
            (directory / 'payload').write_text('reviewed bytes')
            return str(directory)
        if args[0] == '/usr/bin/cp':
            assert '--reflink=never' in args
            kit.shutil.copytree(Path(args[-2]), Path(args[-1]), dirs_exist_ok=True)
        return p.plan['image']
    monkeypatch.setattr(p.host, 'run', run)
    p.original_publish(journal, p.plan)
    root = p.host.path(kit.STORE) / 'rootfs' / p.plan['image'][7:]
    assert (root / '.acc-image-id').read_text() == p.plan['image'] + '\n'
    assert (root / 'payload').read_text() == 'reviewed bytes'
    assert (root / 'payload').stat().st_ino != (p.host.path(kit.STORE) / 'mounted/payload').stat().st_ino
    assert not any('run' in args or 'create' in args or 'exec' in args for args in calls)
    assert any('verify_image_archive.py' in arg for arg in calls[0])
    journal.validate_tree(kit.STORE)


def test_setup_can_enable_linger_before_user_is_known_to_logind(kit, prepared, monkeypatch):
    p = prepared
    original_run = p.host.run
    def run(args, **kwargs):
        if 'show-user' in args and p.state['linger'] == 'no':
            p.calls.append(args)
            raise kit.subprocess.CalledProcessError(1, args, stderr='not logged in or lingering')
        return original_run(args, **kwargs)
    monkeypatch.setattr(p.host, 'run', run)
    journal = kit.setup(p.host, p.plan)
    assert journal.data['status'] == 'ready'
    assert journal.data['settings']['linger']['before'] == 'no'


def test_preexisting_linger_and_sysctl_are_not_disabled_on_uninstall(kit, prepared):
    p = prepared
    p.state['linger'] = 'yes'
    linger = p.host.path('/var/lib/systemd/linger/alex-agent')
    linger.parent.mkdir(parents=True)
    linger.write_text('')
    p.host.path(kit.RESTRICT).write_text('1')
    journal = kit.setup(p.host, p.plan)
    assert journal.data['settings'] == {}
    kit.uninstall(journal)
    assert p.state['linger'] == 'yes'
    assert p.host.path(kit.RESTRICT).read_text() == '1'


def test_root_preflight_disables_bytecode_writes():
    for name in ['preflight', 'setup', 'collect', 'uninstall']:
        assert '/usr/bin/python3 -I -B' in (ROOT / f'scripts/containers-qualification/{name}.sh').read_text()


def test_control_driver_ignores_source_tree_bytecode(kit, prepared, collector):
    journal, state = collector
    assert kit.collect(journal) == 0
    command = next(args for args in prepared.calls if '--phase-1-plan' in args)
    assert '-B' in command
    assert 'PYTHONPYCACHEPREFIX=' + str(journal.folder / 'unused-bytecode') in command


def test_host_commands_refuse_a_writable_executable_before_launch(kit, host):
    executable = host.path('/usr/bin/unsafe')
    executable.parent.mkdir(parents=True)
    executable.write_text('untrusted')
    executable.chmod(0o777)
    with pytest.raises(ValueError, match='unsafe ownership'):
        host.run(['/usr/bin/unsafe', '--version'])


def test_protected_review_source_refuses_unreviewed_bytecode(kit, host, monkeypatch):
    source = host.path('/reviewed')
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/legacy.pyc').write_bytes(b'unreviewed bytecode')
    monkeypatch.setattr(kit, 'SOURCE', source)
    monkeypatch.setattr(kit, 'review_hashes', lambda: {})
    with pytest.raises(ValueError, match='bytecode'):
        kit.source_protected(host)


def test_uninstall_refuses_a_residual_probe_process_outside_launcher_units(kit, prepared):
    p = prepared
    journal = kit.setup(p.host, p.plan)
    probe = p.host.path('/proc/5678')
    probe.mkdir()
    uid = os.getuid()
    (probe / 'status').write_text(f'Uid: {uid} {uid} {uid} {uid}\n')
    (probe / 'cgroup').write_text('0::/user.slice/agent-probe.scope\n')
    policy = p.host.path(kit.POLICY).read_bytes()
    with pytest.raises(ValueError, match='other account processes'):
        kit.uninstall(journal)
    assert p.host.path(kit.POLICY).read_bytes() == policy
    assert p.host.path(kit.STORE).exists()


def test_publish_strips_setuid_and_refuses_devices_before_opening_the_store(kit, prepared, monkeypatch):
    # The image is built by the agent. Root must not publish a world-readable store holding a setuid file,
    # a file capability or a device node from it.
    p = prepared
    journal = kit.Journal.create(p.host)
    p.host.path(p.plan['archive']).write_bytes(b'fake archive')
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'mount' in args:
            directory = p.host.path(kit.STORE) / 'mounted'
            directory.mkdir()
            (directory / 'su').write_text('setuid helper')
            (directory / 'su').chmod(0o6755)
            return str(directory)
        if args[0] == '/usr/bin/cp':
            kit.shutil.copytree(Path(args[-2]), Path(args[-1]), dirs_exist_ok=True)
        return p.plan['image']
    monkeypatch.setattr(p.host, 'run', run)
    p.original_publish(journal, p.plan)
    root = p.host.path(kit.STORE) / 'rootfs' / p.plan['image'][7:]
    assert (root / 'su').stat().st_mode & 0o7777 == 0o755
    assert not any(args[0] == '/usr/bin/chmod' for args in calls[:[a[0] for a in calls].index('/usr/bin/setfacl')])


def test_a_device_node_anywhere_in_the_new_store_stops_setup_with_the_store_closed(kit, host, monkeypatch):
    from types import SimpleNamespace
    store = host.path(kit.STORE)
    store.mkdir(mode=0o700)
    (store / 'disk').write_text('stands in for b 8 0')
    (store / 'whiteout').write_text('stands in for c 0 0')
    real = kit.os.lstat
    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        name = Path(path).name
        if name in ('disk', 'whiteout'):
            kind = kit.stat.S_IFBLK if name == 'disk' else kit.stat.S_IFCHR
            rdev = kit.os.makedev(8, 0) if name == 'disk' else 0
            return SimpleNamespace(st_mode=kind | 0o666, st_rdev=rdev, st_uid=info.st_uid)
        return info
    monkeypatch.setattr(kit.os, 'lstat', lstat)
    with pytest.raises(ValueError, match='device node'):
        kit.harden(store)
    (store / 'disk').unlink()
    kit.harden(store)  # an overlay whiteout (c 0 0) is not a device
    assert kit.stat.S_IMODE(real(store).st_mode) == 0o700


def test_uninstall_runs_only_from_protected_reviewed_source(kit, prepared, monkeypatch):
    # Setup and collect reach this check through preflight; rollback is root code too.
    p = prepared
    journal = kit.setup(p.host, p.plan)
    before = json.dumps(journal.data, sort_keys=True)
    def unprotected(host):
        raise ValueError('unsafe ownership, permissions, symlink or ACL: /reviewed')
    monkeypatch.setattr(kit, 'source_protected', unprotected)
    with pytest.raises(ValueError, match='unsafe ownership'):
        kit.uninstall(kit.Journal.load(p.host))
    assert json.dumps(kit.Journal.load(p.host).data, sort_keys=True) == before
    assert p.state['linger'] == 'yes'


def test_apparmor_loads_and_unloads_leave_no_parser_cache(kit, prepared):
    p = prepared
    journal = kit.setup(p.host, p.plan)
    kit.uninstall(journal)
    parser = [args for args in p.calls if args[0] == '/usr/sbin/apparmor_parser']
    assert parser and all('--skip-cache' in args for args in parser)


def test_unexpected_errors_exit_with_the_unsafe_state_code(kit, prepared, monkeypatch, capsys):
    p = prepared
    monkeypatch.setattr(kit.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(kit, 'read_plan', lambda path, host: p.plan)
    monkeypatch.setattr(kit, 'Host', lambda: p.host)
    def broken(host, plan):
        raise TypeError('unexpected shape')
    monkeypatch.setattr(kit, 'setup', broken)
    monkeypatch.setattr(kit.sys, 'argv', ['kit.py', 'setup', '--plan', '/root/plan.json'])
    assert kit.main() == 2
    assert 'REFUSED' in capsys.readouterr().err


def test_an_unexpected_collector_error_is_a_recorded_no_go(kit, collector, monkeypatch):
    journal, _ = collector
    def broken(host, plan, **kwargs):
        raise TypeError('unexpected shape')
    monkeypatch.setattr(kit, 'preflight', broken)
    assert kit.collect(journal) == 1
    report = json.loads((journal.folder / 'report.json').read_text())
    assert report['go_phase_1'] is False and 'unexpected shape' in report['error']


def test_root_proof_is_built_by_root_from_validated_fields_with_bounded_input(kit, collector):
    journal, state = collector
    assert kit.collect(journal) == 0
    assert state['proof_limit'] == 4096  # the driver refuses larger proofs too
    proof = json.loads(journal.host.path('/etc/acc/containers/qualification/alex-agent.json').read_text())
    assert set(proof) == {'account', 'image', 'time', 'status', 'direct_write', 'unshare_write', 'runtime_layers',
                          'execution_source', 'namespace_boundary', 'files'}
    assert proof['files'] == 3 and proof['time'] > 1.0  # root's capture time, not the agent's


@pytest.mark.parametrize('change', [{'files': 0}, {'files': '3'}, {'files': True}, {'direct_write': 'allowed'},
                                    {'runtime_layers': None}])
def test_root_proof_refuses_any_unproven_field(kit, collector, monkeypatch, change):
    journal, state = collector
    original = journal.host.run
    def run(args, **kwargs):
        if args[0] == '/usr/sbin/runuser' and any('image_store.py' in arg for arg in args):
            return json.dumps({**json.loads(original(args, **kwargs)), **change})
        return original(args, **kwargs)
    monkeypatch.setattr(journal.host, 'run', run)
    assert kit.collect(journal) == 1
    assert not journal.host.path('/etc/acc/containers/qualification/alex-agent.json').exists()


def test_bounded_output_kills_a_command_that_writes_too_much(kit, tmp_path):
    script = 'import sys; sys.stdout.write("x" * 100000); sys.stdout.flush(); import time; time.sleep(30)'
    import time as clock
    started = clock.monotonic()
    with pytest.raises(ValueError, match='exceeds 4096 bytes'):
        kit.bounded(['/usr/bin/python3', '-I', '-c', script], timeout=20, env=kit.ENV, limit=4096)
    assert clock.monotonic() - started < 10
    assert kit.bounded(['/usr/bin/python3', '-I', '-c', 'print("ok")'], timeout=20, env=kit.ENV, limit=4096) == 'ok'
    with pytest.raises(kit.subprocess.TimeoutExpired):
        kit.bounded(['/usr/bin/python3', '-I', '-c', 'import time; time.sleep(30)'], timeout=1, env=kit.ENV, limit=4096)


def test_setup_starts_the_user_manager_before_checking_it(kit, prepared):
    """enable-linger starts user@UID asynchronously; the owner run on acc-host (2026-10-25) refused because
    is-active ran a moment too early. Setup must start (and so wait for) the manager first."""
    p = prepared
    kit.setup(p.host, p.plan)
    commands = [list(c) for c in p.calls if c[:1] == ['/usr/bin/systemctl'] and any(str(a).startswith('user@') for a in c)]
    verbs = [c[1] for c in commands]
    assert 'start' in verbs and 'is-active' in verbs
    assert verbs.index('start') < verbs.index('is-active')

def test_a_failed_host_proof_records_exit_code_and_sanitized_bounded_output(kit, collector):
    # The owner must see which check failed, not only "returned non-zero exit status 1".
    journal, state = collector
    state['proof_failure'] = True
    state['proof_stderr'] = ('x' * (kit.PROBE_STDERR + 5000) + '\n\x1b[31mTraceback\x07 noise\u202e\n'
                             'image_store: FAILED namespace_service_exited: namespace service PID 4321 has exited\n')
    assert kit.collect(journal) == 1
    report = json.loads((journal.folder / 'report.json').read_text())
    proof = report['checks']['image_store']
    assert proof['status'] == 'fail' and proof['exit_code'] == 1
    assert proof['stderr'].endswith('image_store: FAILED namespace_service_exited: namespace service PID 4321 has exited')
    assert len(proof['stderr']) <= kit.PROBE_STDERR and '\x1b' not in proof['stderr'] and '\x07' not in proof['stderr']
    assert '\u202e' not in proof['stderr']
    assert 'namespace_service_exited' in report['error']


def test_bounded_output_keeps_the_tail_of_stderr_for_diagnosis(kit):
    script = 'import sys; sys.stderr.write("e" * 10000 + "\\nlast line\\n"); sys.exit(3)'
    with pytest.raises(kit.subprocess.CalledProcessError) as failure:
        kit.bounded(['/usr/bin/python3', '-I', '-c', script], timeout=20, env=kit.ENV, limit=4096)
    assert failure.value.returncode == 3
    assert failure.value.stderr.endswith('last line\n') and len(failure.value.stderr) <= 4096


def test_namespace_service_outlives_the_probe_timeout(kit):
    # The service is stopped explicitly afterwards; its own lifetime must never end mid-probe.
    assert int(kit.re.search(r"'sleep', '([0-9]+)'", kit.BOOTSTRAP)[1]) > 300 + 60


def fake_process(host, pid, *, comm, uid=None, cgroup='0::/user.slice/session-7.scope', exe=None, args=()):
    proc = host.path(f'/proc/{pid}')
    proc.mkdir(parents=True)
    uid = os.getuid() if uid is None else uid
    (proc / 'status').write_text(f'Uid: {uid} {uid} {uid} {uid}\n')
    (proc / 'comm').write_text(comm + '\n')
    (proc / 'cgroup').write_text(cgroup + '\n')
    (proc / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in (exe or comm, *args)) + b'\0')
    if exe:
        (proc / 'exe').symlink_to(exe)


def pause_pid(host, pid):
    path = host.path(f'/run/user/{os.getuid()}/libpod/tmp/pause.pid')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{pid}\n')


@pytest.mark.parametrize('state', ['none', 'live', 'dead', 'reused', 'malformed'])
def test_preflight_detects_stale_rootless_podman_state_without_running_podman(kit, prepared, state):
    # A killed catatonit leaves pause.pid naming a dead PID: Podman then fails with "invalid internal status".
    p = prepared
    if state != 'none':
        pause_pid(p.host, 'not-a-pid' if state == 'malformed' else 4444)
    if state == 'live':
        fake_process(p.host, 4444, comm='catatonit')
    elif state == 'reused':
        fake_process(p.host, 4444, comm='bash')
    calls = len(p.calls)
    if state in ('none', 'live'):
        assert kit.podman_state(p.host, p.plan['account'])
    else:
        with pytest.raises(ValueError) as failure:
            kit.podman_state(p.host, p.plan['account'])
        message = str(failure.value)
        assert 'stale rootless Podman state' in message and 'podman system migrate' in message
        assert 'runuser -u alex-agent' in message and 'never kill catatonit' in message
    assert len(p.calls) == calls  # read-only: no podman, no command at all
    report = p.original_preflight(p.host, p.plan)
    row = next(c for c in report['checks'] if c['name'] == 'rootless Podman state')
    assert row['status'] == ('pass' if state in ('none', 'live') else 'missing')


def test_census_names_a_stray_podman_pause_process_and_its_fix(kit, prepared):
    p = prepared
    fake_process(p.host, 4444, comm='catatonit')
    pause_pid(p.host, 4444)  # migrate can reach it through its own runtime folder
    fake_process(p.host, 5555, comm='python3')
    with pytest.raises(ValueError) as failure:
        kit.quiet_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert 'Podman pause process' in message and '4444' in message and 'podman system migrate' in message
    assert 'other account processes' in message and '5555' in message
    assert message.index('4444') != message.index('5555')


def user_manager(uid, unit='app.slice/dbus.service'):
    return f'0::/user.slice/user-{uid}.slice/user@{uid}.service/{unit}'


@pytest.mark.parametrize('exe,args,unit', [
    ('/usr/bin/dbus-daemon', ('--session', '--address=systemd:', '--nofork', '--nopidfile', '--systemd-activation'),
     'app.slice/dbus.service'),
    ('/usr/bin/dbus-daemon', ('--session', '--address=systemd:'), 'session.slice/dbus.service'),
    ('/usr/bin/dbus-broker-launch', ('--scope', 'user'), 'session.slice/dbus.service'),
    ('/usr/bin/dbus-broker', ('--log', '4', '--controller', '9', '--machine-id', 'x'), 'session.slice/dbus.service'),
])
def test_census_allows_the_accounts_own_session_bus_in_its_user_manager(kit, prepared, exe, args, unit):
    # Socket-activated in user@UID.service as soon as rootless Podman uses the systemd cgroup manager.
    p = prepared
    fake_process(p.host, 4600, comm=Path(exe).name[:15], exe=exe, args=args, cgroup=user_manager(os.getuid(), unit))
    assert kit.quiet_account(p.host, p.plan['account'])


@pytest.mark.parametrize('change', ['outside_manager', 'other_unit', 'other_user_manager', 'not_session',
                                    'lookalike_binary', 'other_owner'])
def test_census_still_refuses_a_session_bus_lookalike(kit, prepared, change):
    p = prepared
    uid = os.getuid()
    exe, args, cgroup, owner = '/usr/bin/dbus-daemon', ('--session', '--address=systemd:'), user_manager(uid), uid
    if change == 'outside_manager':
        cgroup = '0::/user.slice/user-%d.slice/session-3.scope' % uid
    elif change == 'other_unit':
        cgroup = user_manager(uid, 'app.slice/dbus-lookalike.service')
    elif change == 'other_user_manager':
        cgroup = user_manager(uid + 1)
    elif change == 'not_session':
        args = ('--system',)
    elif change == 'lookalike_binary':
        exe = '/home/alex-agent/bin/dbus-daemon'
    else:
        owner = uid + 1
    fake_process(p.host, 4601, comm='dbus-daemon', exe=exe, args=args, cgroup=cgroup)
    if change == 'other_owner':
        (p.host.path('/proc/4601') / 'status').write_text(f'Uid: {uid} {owner} {uid} {uid}\n')
    with pytest.raises(ValueError, match='4601'):
        kit.quiet_account(p.host, p.plan['account'])


@pytest.mark.parametrize('account_uid', [0, 1000])
def test_census_only_counts_processes_of_the_account_uid_including_root(kit, host, monkeypatch, account_uid):
    # The gate queue runs these tests as root, where the account UID the fixtures use is 0.
    from types import SimpleNamespace
    monkeypatch.setattr(kit.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=account_uid))
    monkeypatch.setattr(host, 'run', lambda args, **kwargs: '999')
    fake_process(host, 999, comm='acc-broker', uid=account_uid, cgroup='0::/system.slice/acc-broker@alex-agent.service')
    service = host.path('/proc/4321')
    service.mkdir(parents=True)
    (service / 'comm').write_text('sleep\n')
    (service / 'status').write_text('Uid: {0} {0} {0} {0}\n'.format(account_uid + 1))
    assert kit.quiet_account(host, 'alex-agent')
    (service / 'status').write_text('Uid: {0} {0} {0} {0}\n'.format(account_uid))  # now the account's: counted
    with pytest.raises(ValueError, match='4321'):
        kit.quiet_account(host, 'alex-agent')


def test_a_failed_host_proof_keeps_podmans_debug_log_in_the_report(kit, collector):
    journal, state = collector
    state['proof_failure'] = True
    debug = ''.join(f'podman-debug: time="t" level=debug msg="{"step %d " % n}{"x" * 250}"\n' for n in range(30))
    state['proof_stderr'] = ('podman: cannot chdir to /root: Permission denied\n' + debug +
                             'image_store: FAILED namespace_unshare: unshare execution-namespace access denial not '
                             'demonstrated (exit 125: Error: setting up the process)\n')
    assert kit.collect(journal) == 1
    proof = json.loads((journal.folder / 'report.json').read_text())['checks']['image_store']
    assert len(proof['podman_debug']) == 30 and proof['podman_debug'][0].startswith('time="t" level=debug msg="step 0')
    assert proof['podman'] == ['cannot chdir to /root: Permission denied']
    assert proof['stderr'].splitlines()[-1].startswith('image_store: FAILED namespace_unshare')
    assert state['proof_stderr_limit'] >= 16384


def test_root_commands_run_from_the_filesystem_root(kit, tmp_path, monkeypatch):
    # The owner's console sits in /root (0700); an agent-identity command must not inherit it.
    monkeypatch.chdir(tmp_path)
    assert kit.bounded(['/usr/bin/python3', '-I', '-c', 'import os; print(os.getcwd())'], timeout=20,
                       env=kit.ENV, limit=4096) == '/'



@pytest.mark.parametrize('folder', ['emptied', 'missing'])
def test_census_gives_the_scope_stop_when_the_pause_processs_runtime_folder_is_gone(kit, prepared, folder):
    """Attempt 10: catatonit lived on in podman-pause-1caba4c2.scope with XDG_RUNTIME_DIR=/run/acc-ct/7a645609ae19ab5b,
    which the launcher had emptied: podman system migrate could no longer reach it, with or without that folder."""
    p = prepared
    uid = os.getuid()
    runtime = '/run/acc-ct/7a645609ae19ab5b'
    fake_process(p.host, 2951658, comm='catatonit',
                 cgroup=user_manager(uid, 'user.slice/podman-pause-1caba4c2.scope'))
    p.host.path('/proc/2951658/environ').write_bytes(
        b'HOME=/home/qual\0XDG_RUNTIME_DIR=' + runtime.encode() + b'\0CONTAINERS_STORAGE_CONF=/srv/acc/containers/storage.conf\0')
    if folder == 'emptied':
        p.host.path(runtime).mkdir(parents=True)
    with pytest.raises(ValueError) as failure:
        kit.quiet_account(p.host, p.plan['account'])
    message = str(failure.value)
    gone = 'no longer holds its pause.pid' if folder == 'emptied' else 'no longer exists'
    assert f'PID 2951658: its XDG_RUNTIME_DIR {runtime} {gone}, so podman system migrate cannot reach it' in message
    assert (f'runuser -u alex-agent -- /usr/bin/env -i XDG_RUNTIME_DIR=/run/user/{uid} PATH=/usr/bin:/bin '
            '/usr/bin/systemctl --user stop podman-pause-1caba4c2.scope (never kill catatonit)') in message
    assert 'system migrate (never' not in message  # no migrate that cannot work


def test_census_never_puts_an_odd_scope_or_path_into_the_fix(kit, prepared):
    p = prepared
    uid = os.getuid()
    fake_process(p.host, 2951659, comm='catatonit', cgroup=user_manager(uid, 'user.slice/evil;rm -rf ~.scope'))
    p.host.path('/proc/2951659/environ').write_bytes(b'XDG_RUNTIME_DIR=/run/acc-ct/x y;$(id)\0')
    with pytest.raises(ValueError) as failure:
        kit.quiet_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert 'XDG_RUNTIME_DIR (unprintable path)' in message and 'rm -rf' not in message and '$(id)' not in message
    assert '/usr/bin/systemctl --user status 2951659 (to find its unit)' in message


@pytest.fixture
def no_hang():
    """A blocking read of an account-made FIFO would hang the owner's tool: fail instead of waiting forever."""
    import signal

    def hung(*_):
        raise AssertionError('blocked on an account-controlled file')
    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize('shape', ['fifo', 'link-to-big-file', 'oversized'])
def test_pause_pid_is_read_bounded_never_through_a_link_or_fifo(kit, prepared, no_hang, tmp_path, shape):
    """Review 2026-10-25 (L-5): pause.pid lives in the account's own folder and was read as root, whole."""
    p = prepared
    path = p.host.path(f'/run/user/{os.getuid()}/libpod/tmp/pause.pid')
    path.parent.mkdir(parents=True, exist_ok=True)
    if shape == 'fifo':
        os.mkfifo(path)
    elif shape == 'link-to-big-file':
        big = tmp_path / 'big'
        big.write_bytes(b'1' * (1 << 20))
        path.symlink_to(big)
    else:
        path.write_text('4' * 4096)
    with pytest.raises(ValueError, match='not a small regular file owned by alex-agent'):
        kit.podman_state(p.host, p.plan['account'])
    # The stray-pause hint reads the runtime dir's pause.pid the same way: a FIFO there gives the scope stop, no hang.
    runtime = f'/run/user/{os.getuid()}'
    fake_process(p.host, 2951660, comm='catatonit', cgroup=user_manager(os.getuid(), 'user.slice/podman-pause-ab12.scope'))
    p.host.path('/proc/2951660/environ').write_bytes(b'XDG_RUNTIME_DIR=' + runtime.encode() + b'\0')
    message = kit.pause_fix(p.host, p.plan['account'], os.getuid(), '2951660')
    assert '/usr/bin/systemctl --user stop podman-pause-ab12.scope' in message


def test_reused_pause_pid_never_copies_the_processs_chosen_name(kit, prepared):
    p = prepared
    pause_pid(p.host, 4444)
    fake_process(p.host, 4444, comm='x;$(id)')
    with pytest.raises(ValueError) as failure:
        kit.podman_state(p.host, p.plan['account'])
    assert 'which is not alex-agent\'s catatonit' in str(failure.value) and '$(id)' not in str(failure.value)


@pytest.mark.parametrize('runtime', [b'run/acc-ct/7a645609ae19ab5b', b'/run/acc-ct/../../etc', b'.'])
def test_a_relative_or_dotdot_runtime_path_gets_the_hint_not_an_error(kit, prepared, runtime):
    """Review 2026-10-25 (L-4): such a path reached host.path(), whose ValueError replaced the whole hint."""
    p = prepared
    uid = os.getuid()
    fake_process(p.host, 2951661, comm='catatonit', cgroup=user_manager(uid, 'user.slice/podman-pause-cd34.scope'))
    p.host.path('/proc/2951661/environ').write_bytes(b'XDG_RUNTIME_DIR=' + runtime + b'\0')
    with pytest.raises(ValueError) as failure:
        kit.quiet_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert 'PID 2951661: its XDG_RUNTIME_DIR (unprintable path) is not a plain absolute path' in message
    assert '/usr/bin/systemctl --user stop podman-pause-cd34.scope (never kill catatonit)' in message
    assert len(runtime) < 2 or runtime.decode() not in message  # the odd path itself is never copied


def test_the_settle_step_uses_the_image_probes_own_podman_environment(kit):
    """`podman system migrate` finds the pause process only through the pause.pid of the same XDG_RUNTIME_DIR."""
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('image_store', ROOT / 'scripts/container_qualification/image_store.py')
    image_store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image_store)
    entry = SimpleNamespace(pw_uid=1006, pw_dir='/home/qual-agent')
    assert kit.probe_env(entry) == image_store.podman_env(entry)


@pytest.fixture
def leftovers(kit, prepared, monkeypatch):
    """Attempt 13 after the image probe: catatonit in a podman-pause scope and the socket-activated session bus."""
    p = prepared
    uid = os.getuid()
    fake_process(p.host, 768520, comm='catatonit', cgroup=user_manager(uid, 'user.slice/podman-pause-c93de532.scope'))
    pause_pid(p.host, 768520)
    fake_process(p.host, 768547, comm='dbus-daemon', exe='/usr/bin/dbus-daemon', args=('--session',),
                 cgroup=user_manager(uid, 'session.slice/dbus.service'))
    scope = p.host.path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/user.slice/podman-pause-c93de532.scope')
    scope.mkdir(parents=True)
    state = {'migrate': True, 'bus': True}
    original = p.host.run

    def run(args, **kwargs):
        if args[0] == '/usr/sbin/runuser':
            p.calls.append(args)
            state.setdefault('options', []).append(kwargs)
            if args[-2:] == ['system', 'migrate'] and state['migrate']:
                shutil.rmtree(p.host.path('/proc/768520'))
                scope.rmdir()
            elif args[-4:] == ['--user', 'stop', 'dbus.socket', 'dbus.service'] and state['bus']:
                shutil.rmtree(p.host.path('/proc/768547'))
            return ''
        return original(args, **kwargs)
    monkeypatch.setattr(p.host, 'run', run)
    return state


def test_settle_ends_the_probes_pause_process_and_session_bus_then_checks(kit, prepared, leftovers):
    p = prepared
    uid = os.getuid()
    with pytest.raises(ValueError):  # before: the strict census refuses both
        kit.quiet_account(p.host, p.plan['account'], bus=False)
    assert 'session bus ended' in kit.settle_account(p.host, p.plan['account'])
    runuser = [args for args in p.calls if args[0] == '/usr/sbin/runuser']
    assert runuser == [
        ['/usr/sbin/runuser', '-u', 'alex-agent', '--', '/usr/bin/env', '-i', 'HOME=/home/alex-agent',
         f'XDG_RUNTIME_DIR=/run/user/{uid}', 'PATH=/usr/bin:/bin', 'CONTAINERS_CONF=/dev/null',
         'CONTAINERS_CONF_OVERRIDE=/dev/null', 'CONTAINERS_STORAGE_CONF=/srv/acc/containers/storage.conf',
         '/usr/bin/podman', 'system', 'migrate'],
        ['/usr/sbin/runuser', '-u', 'alex-agent', '--', '/usr/bin/env', '-i', f'XDG_RUNTIME_DIR=/run/user/{uid}',
         'PATH=/usr/bin:/bin', '/usr/bin/systemctl', '--user', 'stop', 'dbus.socket', 'dbus.service']]


@pytest.mark.parametrize('survivor', ['migrate', 'bus'])
def test_settle_refuses_and_names_what_survived(kit, prepared, leftovers, survivor):
    p = prepared
    uid = os.getuid()
    leftovers[survivor] = False
    with pytest.raises(ValueError) as failure:
        kit.settle_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert message.startswith('the account is not quiet after the image probe: ')
    if survivor == 'migrate':
        assert '768520' in message and 'podman system migrate (never kill catatonit)' in message
        assert ('Podman pause scopes remain in the account\'s user manager: podman-pause-c93de532.scope; fix from the '
                f'owner console: /usr/sbin/runuser -u alex-agent -- /usr/bin/env -i XDG_RUNTIME_DIR=/run/user/{uid} '
                'PATH=/usr/bin:/bin /usr/bin/systemctl --user stop podman-pause-c93de532.scope') in message
        assert '768547' not in message
    else:
        assert "the account's session bus is still running (PIDs 768547)" in message
        assert '--user stop dbus.socket dbus.service, then rerun' in message and '768520' not in message


def test_settle_failing_commands_are_reported_but_the_census_decides(kit, prepared, leftovers, monkeypatch):
    p = prepared
    run = p.host.run

    def failing(args, **kwargs):
        if args[-2:] == ['system', 'migrate']:
            raise kit.subprocess.CalledProcessError(125, args, stderr='account-chosen text')
        return run(args, **kwargs)
    monkeypatch.setattr(p.host, 'run', failing)
    with pytest.raises(ValueError) as failure:
        kit.settle_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert message.startswith('the account is not quiet after the image probe (podman system migrate exited 125): ')
    assert 'account-chosen text' not in message


@pytest.mark.parametrize('name', ['.tmux.conf', '.config/tmux/tmux.conf'])
def test_settle_refuses_a_tmux_configuration_the_brokers_server_would_read(kit, prepared, leftovers, name):
    """The broker starts its tmux server without -f, so the account's tmux.conf could choose the pane's shell, and
    the driver trusts the pane and its descendants."""
    p = prepared
    config = p.host.path('/home/alex-agent') / name
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('set -g default-shell /home/alex-agent/anything\n')
    with pytest.raises(ValueError) as failure:
        kit.settle_account(p.host, p.plan['account'])
    message = str(failure.value)
    assert f'tmux configuration in the account\'s home (~/{name})' in message and 'anything' not in message


def test_settle_runs_the_accounts_commands_bounded_with_a_timeout(kit, prepared, leftovers):
    """Census review L-2: their output is the account's, and stdin must not be the owner's console."""
    kit.settle_account(prepared.host, prepared.plan['account'])
    assert len(leftovers['options']) == 2
    assert all(o.get('limit') and o.get('timeout') == 120 for o in leftovers['options'])


def test_settle_reports_a_command_that_prints_too_much_by_name_only(kit, prepared, leftovers, monkeypatch):
    run = prepared.host.run

    def chatty(args, **kwargs):
        if args[-2:] == ['system', 'migrate']:
            raise ValueError('command output exceeds 4096 bytes')
        return run(args, **kwargs)
    monkeypatch.setattr(prepared.host, 'run', chatty)
    with pytest.raises(ValueError) as failure:
        kit.settle_account(prepared.host, prepared.plan['account'])
    assert '(podman system migrate printed too much)' in str(failure.value)


@pytest.mark.parametrize('case', ['other-process', 'pause-pid-names-the-broker', 'pause-pid-names-a-catatonit-elsewhere'])
def test_settle_runs_migrate_only_when_it_can_end_nothing_but_the_probes_pause_process(kit, prepared, leftovers, case):
    """Census review L-1: migrate stops all of the account's containers and SIGKILLs whatever pause.pid names, so it
    runs only after a census found nothing but the broker, init.scope, the bus and pause processes, and only when
    pause.pid names one of those in a podman-pause scope of the account's user manager."""
    p = prepared
    if case == 'other-process':
        fake_process(p.host, 4700, comm='sleep')
    elif case == 'pause-pid-names-the-broker':
        pause_pid(p.host, 999)
    else:
        fake_process(p.host, 4701, comm='catatonit')  # a login session's, not the probe's scope
        pause_pid(p.host, 4701)
    with pytest.raises(ValueError) as failure:
        kit.settle_account(p.host, p.plan['account'])
    assert not any(args[-2:] == ['system', 'migrate'] for args in p.calls)
    message = str(failure.value)
    if case == 'other-process':
        assert message.startswith('the account is not quiet after the image probe; nothing was stopped: ')
        assert '4700' in message
        assert not any('dbus.socket' in args for args in p.calls)
    else:
        assert 'podman system migrate skipped: pause.pid names no pause process of the probe' in message


@pytest.mark.parametrize('where', ['unit', 'process'])
def test_settle_refuses_a_broker_that_imports_from_the_accounts_home(kit, prepared, leftovers, where):
    """`python -m` from WorkingDirectory=~ puts the account's home first on sys.path; the driver trusts the broker."""
    p = prepared
    if where == 'unit':
        p.state['exec'] = p.state['exec'].replace(' -I -m ', ' -m ')
    else:
        p.host.path('/proc/999/cmdline').write_bytes(b'/srv/acc/app/.venv/bin/python\0-m\0acc.broker.server\0')
    with pytest.raises(ValueError) as failure:
        kit.settle_account(p.host, p.plan['account'])
    assert 'the broker does not start Python isolated (-I)' in str(failure.value)


def test_collector_settles_the_account_after_the_probe_and_before_the_launcher_test(kit, collector, prepared, monkeypatch):
    journal, state = collector
    run = prepared.host.run

    def probe_leaves_its_pause_process(args, **kwargs):  # attempt 13: the probe's catatonit, ended by migrate
        out = run(args, **kwargs)
        if any('image_store.py' in arg for arg in args):
            fake_process(prepared.host, 768520, comm='catatonit',
                         cgroup=user_manager(os.getuid(), 'user.slice/podman-pause-c93de532.scope'))
            pause_pid(prepared.host, 768520)
        elif args[-2:] == ['system', 'migrate']:
            shutil.rmtree(prepared.host.path('/proc/768520'))
        return out
    monkeypatch.setattr(prepared.host, 'run', probe_leaves_its_pause_process)
    assert kit.collect(journal) == 0
    commands = [' '.join(args) for args in prepared.calls]
    probe = next(n for n, c in enumerate(commands) if 'image_store.py' in c)
    migrate = next(n for n, c in enumerate(commands) if c.endswith('podman system migrate'))
    bus = next(n for n, c in enumerate(commands) if c.endswith('--user stop dbus.socket dbus.service'))
    driver = next(n for n, c in enumerate(commands) if '--phase-1-plan' in c)
    assert probe < migrate < bus < driver
    assert 'session bus ended' in json.loads((journal.folder / 'report.json').read_text())['settle']


def test_collector_stops_before_the_launcher_test_when_the_account_stays_busy(kit, collector, prepared):
    journal, state = collector
    fake_process(prepared.host, 768520, comm='catatonit',
                 cgroup=user_manager(os.getuid(), 'user.slice/podman-pause-c93de532.scope'))
    assert kit.collect(journal) == 1
    report = json.loads((journal.folder / 'report.json').read_text())
    assert report['error'].startswith('the account is not quiet after the image probe')
    assert not any('--phase-1-plan' in args for args in prepared.calls)
