"""Fixed private production Codex placement, mandatory offline namespace."""
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


def capture(prepare, *, fixture_root=None):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('owned_codex_pin_probe', Path(__file__).with_name('probe.py'))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    with tempfile.TemporaryDirectory(prefix='relay-codex-placement-') as name:
        output = Path(name)
        output.chmod(0o700)
        assert probe.run(output, ['/usr/bin/codex', '--version']) == probe.CODEX_VERSION
        wrapper = Path('/usr/bin/codex').resolve(strict=True)
        binaries = list(wrapper.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
        assert len(binaries) == 1 and 'sha256:' + hashlib.sha256(binaries[0].read_bytes()).hexdigest() == probe.CODEX_NATIVE_DIGEST
        shutil.copyfile(Path(__file__).with_name('codex_placement_fixture.py'), output / 'driver.py')
        account = pwd.getpwuid(os.getuid())
        (output / 'passwd').write_text(f'{account.pw_name}:x:{os.getuid()}:{os.getgid()}:fixture:/home/probe:/bin/sh\n')
        prepare(output)
        config = json.loads((output / 'config.json').read_text())
        argv = probe.sandbox(output)[:-1]
        for fixture in (config['workspace'], config['run_dir']):
            path = Path(fixture).resolve(strict=True)
            if fixture_root is None:
                assert str(path).startswith('/tmp/acc-pytest-')
            else:
                trusted = Path(fixture_root).resolve(strict=True)
                assert str(trusted).startswith('/tmp/relay-m3-browser-native/')
                assert path.is_relative_to(trusted)
            argv += ['--bind', str(path), str(path)]
        argv += ['--ro-bind', sys.prefix, sys.prefix, '--ro-bind', str(root / 'src'), str(root / 'src'),
            '--dir', '/etc', '--ro-bind', str(output / 'passwd'), '/etc/passwd',
            '--setenv', 'PYTHONPATH', str(root / 'src'), '--chdir', config['workspace'], '--', sys.executable, '/evidence/driver.py']
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=config.get('timeout', 100) + 20, env={'PATH': '/usr/bin:/bin'})
        if result.returncode:
            diagnostics = ''.join((output / name).read_text()[-6000:] for name in ('errors.log', 'native-error.log') if (output / name).exists())
            raise RuntimeError(result.stderr.decode(errors='replace')[-12000:] + diagnostics)
        return json.loads(result.stdout)
