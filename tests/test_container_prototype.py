"""Security contracts for the disconnected phase-0 prototype."""
import io
import os
import subprocess
from pathlib import Path

import pytest

from acc.broker import container_prototype as cp


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def test_flag_defaults_off_and_rejects_unqualified_launch(tmp_path):
    with pytest.raises(cp.PrototypeRefused, match='disabled'):
        cp.probe_command(tmp_path, 'sha256:' + 'a' * 64)
    with pytest.raises(cp.PrototypeRefused, match='digest'):
        cp.probe_command(tmp_path, 'latest', enabled=True)


def test_probe_spec_has_no_host_network_or_secret_metadata(tmp_path, monkeypatch):
    # The spec is built for an agent account; gates may run as root, which the prototype refuses by design.
    monkeypatch.setattr(cp.os, 'getuid', lambda: 1004)
    monkeypatch.setattr(cp.os, 'getgid', lambda: 1004)
    command = cp.probe_command(tmp_path, 'sha256:' + 'a' * 64, enabled=True)
    assert '--network=none' in command
    assert '--read-only' in command
    assert '--cap-drop=ALL' in command
    assert '--security-opt=no-new-privileges' in command
    assert '--pull=never' in command
    assert '--userns=keep-id' in command
    assert not any('unconfined' in arg or '--env' in arg or '--privileged' in arg for arg in command)
    assert command[-2:] == ['/usr/bin/python3', '/qualification/probe.py']


def test_mount_symlinks_and_root_are_refused(tmp_path):
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    for path in (link, Path('/')):
        with pytest.raises(cp.PrototypeRefused, match='directory'):
            cp.probe_command(path, 'sha256:' + 'a' * 64, enabled=True)


def test_private_checkout_has_only_selected_history_and_no_shared_objects(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 'alex@example.com')
    git(source, 'config', 'user.name', 'Alex Example')
    (source / 'file').write_text('selected')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'seed')
    selected = git(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', '-qb', 'sibling')
    (source / 'private').write_text('sibling only')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'private')
    git(source, 'checkout', '-q', selected)
    git(source, 'config', 'core.hooksPath', str(tmp_path / 'evil-hooks'))
    checkout = tmp_path / 'session'
    cp.private_checkout(source, checkout)
    assert git(checkout, 'rev-parse', 'HEAD') == selected
    assert (checkout / '.git').is_dir()
    assert not (checkout / '.git/objects/info/alternates').exists()
    assert not (checkout / 'private').exists()
    assert git(checkout, 'remote') == ''
    assert 'sibling' not in git(checkout, 'show-ref')
    source_objects = {p.stat().st_ino for p in (source / '.git/objects').rglob('*') if p.is_file()}
    assert not any(p.stat().st_ino in source_objects for p in (checkout / '.git/objects').rglob('*') if p.is_file())
    with pytest.raises(cp.PrototypeRefused):
        cp.private_checkout(source, checkout)


def test_credential_transport_accepts_only_acc_issued_ephemeral_material():
    out = io.BytesIO()
    cp.send_credential(out, b'EXAMPLE-SESSION-TOKEN', acc_issued=True, expires_in=60)
    assert out.getvalue() == b'EXAMPLE-SESSION-TOKEN'
    for kwargs in ({'acc_issued': False, 'expires_in': 60}, {'acc_issued': True, 'expires_in': 0},
                   {'acc_issued': True, 'expires_in': 3601}):
        out = io.BytesIO()
        with pytest.raises(cp.PrototypeRefused):
            cp.send_credential(out, b'EXAMPLE', **kwargs)
        assert out.getvalue() == b''


def test_qualification_disabled_has_no_runtime_side_effects(tmp_path):
    import json
    import sys

    script = Path(__file__).resolve().parents[1] / 'scripts/qualify_containers.py'
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert run.returncode == 2
    report = json.loads(run.stdout)
    assert report['go_phase_1'] is False
    assert report['status'] == 'disabled'
    assert report['container_start_ms'] is None


def test_private_checkout_does_not_execute_source_hooks_or_inherited_config(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 'alex@example.com')
    git(source, 'config', 'user.name', 'Alex Example')
    git(source, 'commit', '--allow-empty', '-qm', 'seed')
    marker = tmp_path / 'EXECUTED'
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    for name in ('post-checkout', 'reference-transaction'):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
        hook.chmod(0o755)
    git(source, 'config', 'core.hooksPath', str(hooks))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(hooks))
    cp.private_checkout(source, tmp_path / 'session')
    assert not marker.exists()


def test_empty_repository_failure_removes_only_new_destination(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    destination = tmp_path / 'session'
    with pytest.raises(subprocess.CalledProcessError):
        cp.private_checkout(source, destination)
    assert not destination.exists()
    assert (source / '.git').is_dir()


def test_root_probe_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.os, 'getuid', lambda: 0)
    with pytest.raises(cp.PrototypeRefused, match='root'):
        cp.probe_command(tmp_path, 'sha256:' + 'a' * 64, enabled=True)


@pytest.mark.parametrize('token', [b'', b'x' * 65537])
def test_credential_size_is_bounded(token):
    stream = io.BytesIO()
    with pytest.raises(cp.PrototypeRefused):
        cp.send_credential(stream, token, acc_issued=True, expires_in=60)
    assert stream.getvalue() == b''


def test_qualification_requires_pinned_local_image_before_runtime():
    import json
    import sys

    script = Path(__file__).resolve().parents[1] / 'scripts/qualify_containers.py'
    run = subprocess.run([sys.executable, str(script), '--enable-prototype', '--image',
                          'latest'], capture_output=True, text=True)
    assert run.returncode == 2
    report = json.loads(run.stdout)
    assert report['status'] == 'refused'
    # Root is refused first; otherwise mutable image tags are refused before probing.
    assert ('root' in report['reason']) if os.getuid() == 0 else ('pinned image ID' in report['reason'])
    assert 'rootless_info_exit' not in report


@pytest.mark.parametrize('exit_code,output', [(1, b''), (2, b''), (0, b'{}'),
                                             (2, b'{"hookSpecificOutput":{"permissionDecision":"allow"}}')])
def test_payload_does_not_mistake_helper_crash_for_guard_refusal(exit_code, output):
    import runpy

    payload = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/container_qualification/probe.py'))
    require_refusal = payload['require_guard_refusal']
    with pytest.raises((AssertionError, ValueError, KeyError)):
        require_refusal(subprocess.CompletedProcess([], exit_code, output))


def test_payload_accepts_real_missing_policy_guard_refusal(tmp_path):
    import runpy
    import sys

    root = Path(__file__).resolve().parents[1]
    payload = runpy.run_path(str(root / 'scripts/container_qualification/probe.py'))
    result = subprocess.run([sys.executable, '-I', str(root / 'bin/acc-guard'), 'codex'],
                            input=b'{}', env={'PATH': '/usr/bin:/bin', 'ACC_RUN_DIR': str(tmp_path)},
                            capture_output=True)
    payload['require_guard_refusal'](result)


def _qualify_module():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / 'scripts/qualify_containers.py'
    spec = importlib.util.spec_from_file_location('qualify_containers', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_user_manager_rerun_is_a_transient_user_service_of_this_checkout(tmp_path):
    import argparse

    module = _qualify_module()
    archive = tmp_path / 'image.tar'
    args = argparse.Namespace(image='sha256:' + 'a' * 64, image_archive=archive)
    command, env = module.user_manager_command(args, 1234)
    assert command[:2] == ['systemd-run', '--user'] and '--wait' in command and '--property=Delegate=yes' in command
    assert f'--setenv=PYTHONPATH={module.ROOT / "src"}' in command  # this checkout, not the installed package
    assert command[-6:] == ['--enable-prototype', '--image', 'sha256:' + 'a' * 64, '--image-archive', str(archive),
                            '--launched-by-user-manager']
    assert env['XDG_RUNTIME_DIR'] == '/run/user/1234' and env['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/run/user/1234/bus'
    assert not any(arg in ('--user-manager', '-I') for arg in command)


def test_user_manager_without_linger_is_refused(monkeypatch, capsys):
    import json
    import sys

    module = _qualify_module()
    monkeypatch.setattr(module.os, 'getuid', lambda: 999999)  # no /run/user/999999/bus
    monkeypatch.setattr(sys, 'argv', ['qualify', '--enable-prototype', '--user-manager'])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('nothing may run'))
    assert module.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'refused' and 'enable-linger' in report['reason']


def test_apparmor_profiles_name_only_the_runtime_executables():
    import re

    repo = Path(__file__).resolve().parents[1]
    attached = {}
    for name in ('acc-podman', 'acc-podman-runtime'):
        text = (repo / 'deploy/apparmor' / name).read_text()
        for profile, path in re.findall(r'^profile (\S+) (\S+) flags=\(unconfined\) \{', text, re.M):
            attached[profile] = path
        assert 'userns,' in text
        assert not re.search(r'^\s*(capability|mount|ptrace|change_profile)\b', text, re.M)
    assert attached == {
        'acc-newuidmap': '/usr/bin/newuidmap', 'acc-newgidmap': '/usr/bin/newgidmap',
        'acc-pasta': '/usr/bin/{pasta,pasta.avx2}', 'acc-podman-cli': '/usr/bin/podman', 'acc-crun': '/usr/bin/crun'}
    # the owner installs it in the qualification window; deploy/install.sh never loads it
    loop = re.search(r'for profile in ([^;]+);', (repo / 'deploy/install.sh').read_text()).group(1)
    assert 'acc-podman' not in loop
