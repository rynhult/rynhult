"""Fixed no-account enrolled-node partition proof; no unisolated fallback."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sysconfig
import tempfile
from pathlib import Path


def capture_enrolled_partition(mode='node-partition'):
    if mode not in ('node-partition', 'leader-loss', 'node-replacement'):
        raise ValueError('Unsupported qualification mode')
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('partition_isolation', Path(__file__).with_name('probe.py'))
    isolation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolation)
    dependencies = Path(sysconfig.get_path('purelib')).resolve()
    if not dependencies.is_relative_to(root / '.acc'):
        raise RuntimeError('Use the self-contained locked project test environment for this proof')
    with tempfile.TemporaryDirectory(prefix='relay-enrolled-partition-') as name:
        output = Path(name)
        output.chmod(0o700)
        if isolation.run(output, ['/usr/bin/codex', '--version']) != isolation.CODEX_VERSION:
            raise isolation.QualificationPinMismatch('Installed Codex differs from pin')
        (output / 'mode.json').write_text(json.dumps(mode))
        (output / 'driver.py').write_bytes(Path(__file__).with_name('enrolled_partition.py').read_bytes())
        (output / 'relay_helpers.py').write_bytes((root / 'tests/relay_helpers.py').read_bytes())
        (output / 'passwd').write_text(f'relayagent:x:{os.getuid()}:{os.getgid()}:Offline fixture:/home/probe:/bin/sh\n')
        (output / 'group').write_text(f'relayagent:x:{os.getgid()}:\n')
        command = isolation.sandbox(output)[:-1] + [
            '--ro-bind', str(root / 'src'), '/fixture/src', '--ro-bind', str(dependencies), '/deps',
            '--dir', '/etc', '--ro-bind', str(output / 'passwd'), '/etc/passwd',
            '--ro-bind', str(output / 'group'), '/etc/group',
            '--setenv', 'PYTHONPATH', '/fixture/src:/deps:/evidence', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--', '/usr/bin/python3', '/evidence/driver.py']
        result = subprocess.run(command, env={'PATH':'/usr/bin:/bin'}, capture_output=True, timeout=150)
        if result.returncode:
            raise RuntimeError('Offline enrolled partition proof failed: ' + result.stderr.decode(errors='replace')[-12000:])
        return json.loads((output / 'result.json').read_text())


if __name__ == '__main__':
    print(json.dumps(capture_enrolled_partition(), indent=2))
