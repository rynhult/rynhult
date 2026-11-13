"""Generated-only Claude fixture homes; no host account settings are mounted."""
import hashlib
import json
import subprocess
from pathlib import Path


def capture_temporary_claude_tui(tmp_path):
    root = Path(__file__).resolve().parents[2]
    lab = Path(tmp_path).resolve() / 'native-lab'
    lab.mkdir(mode=0o700)
    fixture = lab / 'lab'
    fixture.mkdir(mode=0o700)
    artifacts = fixture / 'artifacts'
    artifacts.mkdir(mode=0o700)
    for name in ('home', 'config'):
        (fixture / name).mkdir(mode=0o700)
    # Reuse the reviewed synthetic model/PTY protocol. Only its fixed fixture
    # namespace prefixes change; no installed CLI or production class is patched.
    source = Path(__file__).with_name('claude_resume.py').read_text()
    assert '/home/probe' in source and '/evidence' in source
    source = source.replace('/home/probe', '/tmp/lab/home').replace('/evidence', '/tmp/lab/artifacts')
    marker = "{'hooks': {'PreToolUse':"
    assert source.count(marker) == 1
    source = source.replace(marker, "{'disableAllHooks': False, 'hooks': {'PreToolUse':")
    marker = 'if len(requests) == 1:'
    assert source.count(marker) == 1
    source = source.replace(marker, "if len(requests) == 1 or (messages and messages[-1]['content'] == ['Native TUI isolated fixture turn']):")
    # This config belongs only to the newly generated temporary account. The
    # native command's frozen --settings must keep required hooks enabled.
    (fixture / 'config/settings.json').write_text(json.dumps({'disableAllHooks':True}))
    marker = "assert not any(Path(os.environ['CLAUDE_CONFIG_DIR']).iterdir())"
    assert source.count(marker) == 1
    source = source.replace(marker,
        "assert json.loads((Path(os.environ['CLAUDE_CONFIG_DIR']) / 'settings.json').read_text()) == {'disableAllHooks': True}")
    prelude = '''
def confinement():
    import os, stat
    from pathlib import Path
    assert os.environ['HOME'] == '/tmp/lab/home'
    assert os.environ['CLAUDE_CONFIG_DIR'] == '/tmp/lab/config'
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    writable = [row[4] for row in mounts if 'rw' in row[5].split(',')
                and not row[4].startswith(('/proc', '/dev/pts'))
                and not stat.S_ISCHR(Path(row[4]).stat().st_mode)]
    assert writable == ['/tmp'], writable
    assert not Path('/home').exists() and not Path('/srv').exists()
    return {'writableFileMounts':writable, 'noHostAccountMounted':True,
            'home':'/tmp/lab/home', 'configDir':'/tmp/lab/config'}
CONFINEMENT = confinement()
'''
    marker = '\ndef main():\n'
    assert source.count(marker) == 1
    source = source.replace(marker, prelude + marker)
    marker = "print(json.dumps({'results': results,"
    assert source.count(marker) == 1
    source = source.replace(marker, "print(json.dumps({'confinement':CONFINEMENT, 'results': results,")
    (artifacts / 'driver.py').write_text(source)
    for original, target in [('bin/acc-guard', 'guard_source.py'), ('src/acc/env.py', 'acc_env.py'),
                             ('src/acc/broker/pty_exec.py', 'pty_exec.py')]:
        (artifacts / target).write_bytes((root / original).read_bytes())
    for name in ('use-enclosure', 'tui-mode'):
        (artifacts / name).touch()
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
    for name in ('usr', 'bin', 'lib', 'lib64'):
        if Path('/' + name).exists():
            command += ['--ro-bind', '/' + name, '/' + name]
    command += ['--proc', '/proc', '--dev', '/dev', '--remount-ro', '/dev',
        '--bind', str(lab), '/tmp', '--ro-bind', str(root / 'src'), '/fixture/src',
        '--remount-ro', '/', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
        '--setenv', 'HOME', '/tmp/lab/home', '--setenv', 'CLAUDE_CONFIG_DIR', '/tmp/lab/config',
        '--setenv', 'PYTHONPATH', '/fixture/src', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
        '--chdir', '/tmp/lab/home', '--']
    def run(args, timeout):
        result = subprocess.run(command + args, env={'PATH':'/usr/bin:/bin'},
            capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Temporary Claude fixture failed: ' + result.stderr.decode(errors='replace')[-8000:])
        return result.stdout.decode()
    version = run(['/usr/bin/claude', '--version'], 15).strip()
    assert version == '2.1.308 (Claude Code)'
    digest = 'sha256:' + hashlib.sha256(Path('/usr/bin/claude').read_bytes()).hexdigest()
    assert digest == 'sha256:356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58'
    result = json.loads(run(['/usr/bin/python3', '/tmp/lab/artifacts/driver.py'], 80))
    result.update(cli_version=version, cli_digest=digest,
        generatedPaths=[str(path.relative_to(lab)) for path in sorted(lab.rglob('*')) if path.is_file()],
        hostWritesConfinedTo=str(lab), protectedSettingsAccessed=False,
        generatedDisableHooksOverridden=True)
    return result
