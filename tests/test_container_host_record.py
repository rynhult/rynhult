"""Containers are a Preview that needs a qualified host: acc.container_host_record, the record that lets an owner switch
a project to containers. Run as this test's user, so its uid stands in for root and its folder for /."""
import hashlib
import json
import os

import pytest

from acc import container_host_record as hr

MACHINE = 'a' * 32


def passing_report(**changes):
    return {'version': 1, 'status': 'scoped-phase1-pass', 'go_phase_1': True, 'production_qualified': False,
            'checks': {}, **changes}


@pytest.fixture
def host(tmp_path):
    root = tmp_path / 'root'
    (root / 'etc/acc/containers').mkdir(parents=True)
    for folder in (root, root / 'etc', root / 'etc/acc', root / 'etc/acc/containers'):
        folder.chmod(0o755)
    (root / 'etc/machine-id').write_text(MACHINE + '\n')
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(passing_report()))
    args = dict(path=root / 'etc/acc/containers/host-qualified.json', machine_id=root / 'etc/machine-id',
                root_uid=os.getuid(), top=root)
    return root, report, args


def test_no_record_means_not_qualified_with_how_to_qualify(host):
    _, _, args = host
    result = hr.check(**args)
    assert result['passed'] is False
    assert "hasn't recorded a passed container qualification" in result['reason']
    assert 'acc.container_host_record record report.json' in result['reason']


def test_a_passing_report_recorded_for_this_machine_passes(host):
    _, report, args = host
    entry = hr.record(report, **args)
    assert entry['report_sha256'] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert entry['machine_id'] == MACHINE and oct(args['path'].stat().st_mode & 0o777) == '0o644'
    result = hr.check(**args)
    assert result['passed'] is True and result['report_sha256'] == entry['report_sha256']
    assert result['status'] == 'scoped-phase1-pass'


@pytest.mark.parametrize('report', [passing_report(go_phase_1=False, status='no-go'), passing_report(status='no-go'),
                                    passing_report(go_phase_1='true'), ['not', 'a', 'report']])
def test_a_failing_report_records_nothing(host, tmp_path, report):
    _, _, args = host
    failing = tmp_path / 'failing.json'
    failing.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="didn't pass"):
        hr.record(failing, **args)
    assert not args['path'].exists()


def test_only_root_records(host, monkeypatch):
    _, report, args = host
    monkeypatch.setattr(os, 'geteuid', lambda: args['root_uid'] + 1)
    with pytest.raises(SystemExit, match='runs as root'):
        hr.record(report, **args)


def test_a_record_from_another_machine_does_not_count(host):
    root, report, args = host
    hr.record(report, **args)
    (root / 'etc/machine-id').write_text('b' * 32 + '\n')
    result = hr.check(**args)
    assert result['passed'] is False and 'from another machine' in result['reason']


@pytest.mark.parametrize('change, reason', [
    (lambda path, root: path.chmod(0o666), "isn't trusted (not owned by root, or writable by someone else)"),
    (lambda path, root: (root / 'etc/acc/containers').chmod(0o777), 'its folder'),
    (lambda path, root: (path.unlink(), path.symlink_to(root / 'etc/machine-id')), 'not a regular file'),
    (lambda path, root: path.write_text(json.dumps({**json.loads(path.read_text()), 'go_phase_1': False})),
     "doesn't name a passing report"),
    (lambda path, root: path.write_text(json.dumps({**json.loads(path.read_text()), 'report_sha256': 'x'})),
     "doesn't name a passing report"),
    (lambda path, root: path.write_text('{'), "can't be read"),
])
def test_an_untrusted_or_wrong_record_leaves_containers_off(host, change, reason):
    root, report, args = host
    hr.record(report, **args)
    change(args['path'], root)
    result = hr.check(**args)
    assert result['passed'] is False and reason in result['reason'], result


def test_a_record_owned_by_someone_other_than_root_does_not_count(host):
    _, report, args = host
    hr.record(report, **args)
    assert hr.check(**{**args, 'root_uid': args['root_uid'] + 1})['passed'] is False


def test_record_refuses_a_folder_others_can_write(host):
    root, report, args = host
    (root / 'etc/acc').chmod(0o777)
    with pytest.raises(SystemExit, match='writable by nobody else'):
        hr.record(report, **args)



def test_privileged_record_instructions_use_isolated_python():
    assert 'python -I -m acc.container_host_record' in hr.HOW


def test_unreadable_record_fails_closed(host, monkeypatch):
    _, report, args = host
    hr.record(report, **args)
    monkeypatch.setattr(hr.os, 'lstat', lambda path: (_ for _ in ()).throw(PermissionError('denied')))
    assert hr.check(**args)['passed'] is False


def test_json_recursion_failure_is_refused(host, monkeypatch):
    _, report, args = host
    hr.record(report, **args)

    def recursion(data):
        raise RecursionError("record exceeds this decoder's nesting limit")

    monkeypatch.setattr(hr.json, 'loads', recursion)
    assert hr.check(**args)['passed'] is False


def test_record_command_ignores_planted_cwd_and_pythonpath(host, tmp_path):
    import shlex
    import subprocess
    import venv
    from pathlib import Path

    runtime = tmp_path / 'runtime'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    package = next((runtime / 'lib').glob('python*/site-packages')) / 'acc'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'container_host_record.py').write_bytes(Path(hr.__file__).read_bytes())
    checkout = tmp_path / 'agent-checkout'
    checkout.mkdir()
    marker = tmp_path / 'executed'
    (checkout / 'json.py').write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('planted')\n")
    command = shlex.split(hr.HOW.split('`')[1])
    flags = command[2:command.index('-m') + 1]
    result = subprocess.run([str(runtime / 'bin/python'), *flags, 'acc.container_host_record', '--help'],
                            cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout)}, capture_output=True, timeout=10)
    assert not marker.exists(), 'the privileged instructions executed agent-controlled Python'
    assert result.returncode == 0, result.stderr.decode()


def test_record_refuses_symlink_report(host):
    _, report, args = host
    link = report.with_name('link.json')
    link.symlink_to(report)
    with pytest.raises(SystemExit, match='regular'):
        hr.record(link, **args)
    assert not args['path'].exists()


def test_record_refuses_fifo_without_waiting_for_a_writer(host):
    import subprocess
    import sys
    from pathlib import Path

    _, report, args = host
    fifo = report.with_name('report.fifo')
    os.mkfifo(fifo)
    script = ('import os, sys; from pathlib import Path; from acc import container_host_record as hr; '
              'hr.record(Path(sys.argv[1]), path=Path(sys.argv[2]), machine_id=Path(sys.argv[3]), '
              'root_uid=os.getuid(), top=Path(sys.argv[4]))')
    result = subprocess.run([sys.executable, '-c', script, str(fifo), str(args['path']), str(args['machine_id']),
                             str(args['top'])], env={**os.environ, 'PYTHONPATH': str(Path(hr.__file__).parents[1])},
                            capture_output=True, timeout=5)
    assert result.returncode != 0 and b'regular file' in result.stderr
    assert not args['path'].exists()


def test_opened_record_permissions_are_rechecked(host, monkeypatch):
    _, report, args = host
    hr.record(report, **args)
    original = hr._trusted

    def changed_after_check(path, root_uid, top):
        result = original(path, root_uid, top)
        path.chmod(0o666)
        return result

    monkeypatch.setattr(hr, '_trusted', changed_after_check)
    assert hr.check(**args)['passed'] is False
