"""Group5/6 fixed native qualification in an enforced offline namespace."""
import hashlib
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def capture(prepare, *, timeout=180):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('lifetime_pin_probe', Path(__file__).with_name('probe.py'))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    with tempfile.TemporaryDirectory(prefix='relay-lifetime-') as name:
        output = Path(name)
        output.chmod(0o700)
        assert probe.run(output, ['/usr/bin/codex', '--version']) == probe.CODEX_VERSION
        wrapper = Path('/usr/bin/codex').resolve(strict=True)
        binaries = list(wrapper.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
        assert len(binaries) == 1 and 'sha256:' + hashlib.sha256(binaries[0].read_bytes()).hexdigest() == probe.CODEX_NATIVE_DIGEST
        # Freeze installed tool bytes for this isolated run: a host package
        # replacement must not remove the executable halfway through churn.
        # This is a temporary fixture artifact, never a machine installation.
        package = wrapper.parent.parent
        snapshot = output / 'node_modules/@openai/codex'
        snapshot.parent.mkdir(parents=True, mode=0o700)
        shutil.copytree(package, snapshot, symlinks=True)
        frozen = snapshot / binaries[0].relative_to(package)
        assert 'sha256:' + hashlib.sha256(frozen.read_bytes()).hexdigest() == probe.CODEX_NATIVE_DIGEST
        shutil.copyfile(Path(__file__).with_name('lifetime_fixture.py'), output / 'driver.py')
        shutil.copyfile(Path(__file__).with_name('main_restart_worker.py'), output / 'main_driver.py')
        account = pwd.getpwuid(os.getuid())
        (output / 'passwd').write_text(f'{account.pw_name}:x:{os.getuid()}:{os.getgid()}:fixture:/home/probe:/bin/sh\n')
        prepare(output)
        config = json.loads((output / 'config.json').read_text())
        argv = probe.sandbox(output)[:-1] + ['--ro-bind', str(output / 'node_modules'), '/usr/lib/node_modules',
            '--ro-bind', str(output / 'node_modules'), '/evidence/node_modules']
        for fixture in (config['workspace'], config['run_dir'], *([config['main_root']] if config.get('main_root') else [])):
            path = Path(fixture).resolve(strict=True)
            assert str(path).startswith('/tmp/acc-pytest-')
            argv += ['--bind', str(path), str(path)]
        argv += ['--ro-bind', sys.prefix, sys.prefix, '--ro-bind', str(root / 'src'), str(root / 'src'),
            '--dir', '/etc', '--ro-bind', str(output / 'passwd'), '/etc/passwd',
            '--setenv', 'PYTHONPATH', str(root / 'src'), '--chdir', config['workspace'], '--', sys.executable, '/evidence/driver.py']
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env={'PATH': '/usr/bin:/bin'})
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace')[-16000:])
        return json.loads(result.stdout)
