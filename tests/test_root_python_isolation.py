"""Root Python must not search caller-controlled import locations."""
import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# These are imported modules, not programs. All other deploy/bin Python files
# are checked, including installer-only tools without executable mode bits.
MODULES = {'bin/acc_env.py', 'deploy/telemetry/app.py', 'deploy/containers/sandbox_policy.py',
           'deploy/containers/session_runtime.py'}


def python_programs():
    for folder in ('deploy', 'bin'):
        for path in sorted((REPO / folder).rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(REPO).as_posix()
            if relative in MODULES:
                continue
            first = path.read_bytes().split(b'\n', 1)[0]
            if path.suffix == '.py' or (first.startswith(b'#!') and b'python' in first):
                yield relative


@pytest.mark.parametrize('program', list(python_programs()))
def test_root_python_programs_have_isolated_system_shebangs(program):
    first = (REPO / program).read_text().splitlines()[0]
    assert first.startswith('#!'), program
    argv = shlex.split(first[2:])
    assert argv[0] == '/usr/bin/python3', program
    assert len(argv) == 2 and argv[1].startswith('-') and 'I' in argv[1], program


def test_linux_shell_and_container_python_calls_are_isolated():
    checked = []
    for path in sorted((REPO / 'deploy').rglob('*')) + sorted((REPO / 'bin').iterdir()):
        if not path.is_file() or any(part in ('macos', 'windows') for part in path.parts):
            continue
        text = path.read_text(errors='replace')
        if not (text.startswith(('#!/bin/sh', '#!/bin/bash', '#!/usr/bin/env bash')) or path.name == 'Containerfile'):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith('#'):
                continue
            for match in re.finditer(r'(?<![\w/-])(?P<exe>(?:/[\w./-]+/)?python3?|\$python|\$PYTHON)"?\s+', line):
                # Skip package names, assignments and interpreter paths passed to uv.
                args = line[match.end():]
                if not (args.startswith('-') or re.match(r'["\w/$][^\s]*\.py(?:\s|"|$)', args)):
                    continue
                checked.append(f'{path.relative_to(REPO)}:{number}')
                assert re.match(r'-[A-Za-z]*I[A-Za-z]*(?:\s|$)', args), f'{checked[-1]}: {line.strip()}'
                assert match['exe'].startswith(('/usr/bin/python3', '/opt/acc-telemetry/venv/bin/python', '$')), checked[-1]
    assert checked, 'no root interpreter calls discovered'


def test_root_unit_python_commands_are_isolated():
    deploy = REPO / 'deploy'
    for path in deploy.rglob('*'):
        if not path.is_file() or '.service' not in path.name:
            continue
        lines = path.read_text().splitlines()
        if path.suffix == '.conf':
            base = path.name.split('.service', 1)[0] + '.service'
            inherited = [line for unit in deploy.rglob(base) for line in unit.read_text().splitlines()
                         if line.startswith('User=')]
            lines = inherited + lines
        users = [line.partition('=')[2] for line in lines if line.startswith('User=')]
        for line in lines:
            if not re.match(r'Exec(?:Start|Stop|Reload|Condition)(?:Pre|Post)?=', line):
                continue
            command = line.partition('=')[2]
            if users and users[-1] not in ('root', '0') and not command.startswith(('+', '!')):
                continue
            argv = shlex.split(command.lstrip('-+!:@'))
            if not argv:
                continue
            if any(re.search(r'\bpython(?:3|\d+\.\d+)?\b', arg) for arg in argv):
                assert argv[:2] == ['/usr/bin/python3', '-I'], f'{path}: {line}'
            else:
                # Installed helpers retain their source basename.
                sources = [p for p in (REPO / 'deploy').rglob(Path(argv[0]).name) if p.is_file()]
                for source in sources:
                    if source.read_bytes().startswith(b'#!/usr/bin') and 'python' in source.read_text().splitlines()[0]:
                        assert 'I' in shlex.split(source.read_text().splitlines()[0][2:])[1], source


def test_accounts_entrypoint_ignores_pythonpath_user_site_and_working_directory(tmp_path):
    script = tmp_path / 'sbin/acc-accounts'
    script.parent.mkdir()
    shutil.copy2(REPO / 'deploy/accounts/acc-accounts', script)
    shutil.copy2(REPO / 'src/acc/env.py', script.with_name('acc_env.py'))
    shadow = tmp_path / 'shadow'
    shadow.mkdir()
    marker = tmp_path / 'injected'
    payload = f'from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError("injected")\n'
    user_site = tmp_path / f'user/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    for folder in (script.parent, shadow, user_site):
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('json.py', 'sitecustomize.py', 'usercustomize.py'):
            (folder / name).write_text(payload)
    result = subprocess.run([str(script)], cwd=shadow, capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'PYTHONPATH': str(shadow), 'PYTHONUSERBASE': str(tmp_path / 'user'),
                                 'PYTHONHOME': str(tmp_path / 'not-python'), 'ACC_ACCOUNTS_ROOT': str(tmp_path / 'accounts')})
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize('mode', [0o664, 0o666])
def test_device_recovery_refuses_writable_signer_before_execution(tmp_path, mode, monkeypatch):
    target = tmp_path / 'acc-device-ca.py'
    marker = tmp_path / 'executed'
    target.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    target.chmod(mode)
    recovery = tmp_path / 'acc-device-ca-recovery.py'
    shutil.copy2(REPO / 'deploy/device-ca/acc-device-ca-recovery.py', recovery)
    namespace = {'__file__': str(recovery), '__name__': 'test_recovery'}
    exec(compile(recovery.read_text(), str(recovery), 'exec'), namespace)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    real = Path.lstat

    def root_stat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        return os.stat_result((*tuple(info)[:4], 0, 0, *tuple(info)[6:]))

    monkeypatch.setattr(Path, 'lstat', root_stat)
    with pytest.raises(ValueError, match='unsafe.*signer'):
        namespace['signer']()
    assert not marker.exists()


def test_python_inventory_does_not_mistake_modules_for_entrypoints():
    for relative in MODULES:
        tree = ast.parse((REPO / relative).read_text())
        assert not any(isinstance(node, ast.If) and '__main__' in ast.unparse(node.test) for node in tree.body), relative


def test_image_preparation_loads_its_explicit_package_under_isolation(tmp_path):
    package = tmp_path / 'acc'
    providers = package / 'providers'
    providers.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (providers / '__init__.py').write_text('def get(name):\n    return "loaded " + name\n')
    code = """import runpy,sys
from pathlib import Path
tool = runpy.run_path(sys.argv[1])
tool['load_providers'](Path(sys.argv[2]))
from acc.providers import get
print(get('codex'))
"""
    result = subprocess.run([sys.executable, '-I', '-c', code,
                             str(REPO / 'deploy/containers/prepare_session.py'), str(package)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'loaded codex\n'


@pytest.mark.parametrize('unsafe', [None, 'writable-code', 'foreign-code', 'writable-parent', 'sticky-package'])
def test_updater_loads_only_protected_explicit_source(tmp_path, unsafe):
    script = tmp_path / 'sbin/acc-update'
    script.parent.mkdir()
    shutil.copy2(REPO / 'deploy/updates/acc-update', script)
    shutil.copy2(REPO / 'src/acc/env.py', script.with_name('acc_env.py'))
    source = tmp_path / 'source'
    package = source / 'src/acc'
    package.mkdir(parents=True)
    marker = tmp_path / 'executed'
    (package / '__init__.py').write_text('')
    target = package / 'update_helper.py'
    target.write_text(f'from pathlib import Path\ndef main():\n    Path({str(marker)!r}).touch()\n    return 0\n')
    for path in (script, script.with_name('acc_env.py'), package / '__init__.py', target):
        path.chmod(0o644)
    if unsafe == 'writable-code':
        target.chmod(0o666)
    if unsafe == 'writable-parent':
        source.chmod(0o775)
    if unsafe == 'sticky-package':
        package.chmod(0o1777)
    # Simulate root-owned installation metadata, never acquire privileges or run
    # the real transaction runner. Only the tiny test package can be executed.
    code = """import os,runpy,sys
from pathlib import Path
real = Path.stat
def root_stat(path, *args, **kwargs):
    info = real(path, *args, **kwargs)
    owner = 1 if sys.argv[2] == 'foreign-code' and str(path) == sys.argv[3] else 0
    return os.stat_result((*tuple(info)[:4], owner, 0, *tuple(info)[6:]))
Path.stat = root_stat
os.geteuid = lambda: 0
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(script), str(unsafe), str(target)],
                            env={**os.environ, 'ACC_UPDATE_SOURCE': str(source)}, capture_output=True, text=True, timeout=10)
    if unsafe:
        assert result.returncode == 2, result.stderr
        assert 'unsafe update source' in result.stderr
        assert not marker.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert marker.exists()


@pytest.mark.parametrize('mode', [0o644, 0o666])
def test_image_wrapper_loads_only_checked_absolute_policy(tmp_path, mode):
    import runpy

    binary = tmp_path / 'codex'
    binary.write_text('binary')
    runpy.run_path(str(REPO / 'deploy/containers/install_wrappers.py'))['wrap'](binary, 'codex')
    policy = tmp_path / 'sandbox_policy.py'
    policy.write_text('def execute(*args):\n    print("trusted policy")\n')
    policy.chmod(mode)
    binary.write_text(binary.read_text().replace('/opt/acc/runtime/sandbox_policy.py', str(policy)))
    code = """import os,sys
from pathlib import Path
real = Path.stat
def root_stat(path, *args, **kwargs):
    info = real(path, *args, **kwargs)
    return os.stat_result((*tuple(info)[:4], 0, 0, *tuple(info)[6:]))
Path.stat = root_stat
exec(compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec'))
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(binary)],
                            capture_output=True, text=True, timeout=10)
    if mode == 0o644:
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'trusted policy\n'
    else:
        assert result.returncode != 0
        assert 'unsafe container policy' in result.stderr
        assert result.stdout == ''


@pytest.mark.parametrize('owner,uid_map,runs', [
    ('overflow', '0 1 1000\n1000 0 1\n1001 1001 64536', True),  # attempt 15: root's files in the rootless container
    ('overflow', '0 0 4294967295', False),                       # the initial namespace: the real nobody
    ('other', '0 1 1000\n1000 0 1\n1001 1001 64536', False),
])
def test_image_wrapper_takes_host_roots_files_in_the_rootless_container_as_roots(tmp_path, owner, uid_map, runs):
    import runpy

    binary = tmp_path / 'codex'
    binary.write_text('binary')
    runpy.run_path(str(REPO / 'deploy/containers/install_wrappers.py'))['wrap'](binary, 'codex')
    policy = tmp_path / 'sandbox_policy.py'
    policy.write_text('def execute(*args):\n    print("trusted policy")\n')
    policy.chmod(0o644)
    (tmp_path / 'uid_map').write_text(uid_map + '\n')
    (tmp_path / 'overflowuid').write_text('65534\n')
    text = binary.read_text()
    assert '/proc/self/uid_map' in text and '/proc/sys/kernel/overflowuid' in text
    binary.write_text(text.replace('/opt/acc/runtime/sandbox_policy.py', str(policy))
                      .replace('/proc/self/uid_map', str(tmp_path / 'uid_map'))
                      .replace('/proc/sys/kernel/overflowuid', str(tmp_path / 'overflowuid')))
    code = """import os,sys
from pathlib import Path
real = Path.stat
owner = 65534 if sys.argv[2] == 'overflow' else 1001
def foreign_stat(path, *args, **kwargs):
    info = real(path, *args, **kwargs)
    return os.stat_result((*tuple(info)[:4], owner, 0, *tuple(info)[6:]))
Path.stat = foreign_stat
os.getuid = lambda: 1000
exec(compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec'))
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(binary), owner],
                            capture_output=True, text=True, timeout=10)
    if runs:
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'trusted policy\n'
    else:
        assert result.returncode != 0 and 'unsafe container policy' in result.stderr


def test_image_wrapper_carries_the_policys_own_ownership_rule(tmp_path):
    """L-3 (phase1-in-container independent review): the wrapper can't load the policy before trusting it, so it carries
    sandbox_policy's root_uids() and INITIAL_MAP verbatim, copied at image build; a changed rule changes both."""
    import ast
    import runpy

    tools = runpy.run_path(str(REPO / 'deploy/containers/install_wrappers.py'))
    source = (REPO / 'deploy/containers/sandbox_policy.py').read_text()
    binary = tmp_path / 'codex'
    binary.write_text('binary')
    tools['wrap'](binary, 'codex')
    text = binary.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'root_uids' or (
                isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'INITIAL_MAP'):
            assert ast.get_source_segment(source, node) in text
    wrapper = ast.parse(text)
    assert [n.name for n in wrapper.body if isinstance(n, ast.FunctionDef)] == ['root_uids']
    assert text.count('overflowuid') == source.count('overflowuid') and '4294967295' not in text.replace(
        tools['ownership_rule'](source), '')

    # A different rule in the policy is the wrapper's rule too: drift can't happen.
    changed = source.replace('    uids = {0}\n', '    uids = {0, 4242}\n')
    assert changed != source
    other = tmp_path / 'chrome'
    other.write_text('binary')
    tools['wrap'](other, 'chrome', changed)
    assert 'uids = {0, 4242}' in other.read_text()
    with pytest.raises(SystemExit, match='INITIAL_MAP and root_uids'):
        tools['ownership_rule'](source.replace('def root_uids(', 'def other_uids('))


def test_image_copilot_command_is_the_checked_wrapper_running_node(tmp_path):
    """Attempt 16: Copilot started without hooks. Its command is now the same checked wrapper as Codex's and Chrome's,
    so sandbox_policy decides (copilot_problem) before the pinned Node runs the package."""
    import runpy

    wrapper = tmp_path / 'copilot'
    runpy.run_path(str(REPO / 'deploy/containers/install_wrappers.py'))['write'](wrapper, 'copilot', '/usr/local/bin/node')
    assert wrapper.stat().st_mode & 0o777 == 0o755
    policy = tmp_path / 'sandbox_policy.py'
    policy.write_text('def execute(kind, real, args):\n    print(kind, real, args)\n')
    policy.chmod(0o644)
    wrapper.write_text(wrapper.read_text().replace('/opt/acc/runtime/sandbox_policy.py', str(policy)))
    code = """import os,sys
from pathlib import Path
real = Path.stat
def root_stat(path, *args, **kwargs):
    info = real(path, *args, **kwargs)
    return os.stat_result((*tuple(info)[:4], 0, 0, *tuple(info)[6:]))
Path.stat = root_stat
sys.argv = [sys.argv[1], '-p', 'x']
exec(compile(Path(sys.argv[0]).read_bytes(), sys.argv[0], 'exec'))
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(wrapper)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "copilot /usr/local/bin/node ['-p', 'x']\n"
    assert wrapper.read_text().startswith('#!/usr/bin/python3 -IB\n')
