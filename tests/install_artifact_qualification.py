"""Qualification-only test controls; never imported by production code."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from acc import install_artifact as artifact

FIXTURES = Path(__file__).parent / 'fixtures/install-artifact'


OPTIONS = ('install_qualification', 'install_accd_python', 'install_broker_python', 'install_qualification_record')


def capture_environment(config, environ, inherited=None):
    """Preserve only test qualification controls before host isolation scrubs env."""
    inherited = inherited or {}
    for name in OPTIONS:
        supplied = environ.get('ACC_' + name.upper())
        if name == 'install_qualification':
            value = config.getoption(name) or inherited.get(name) is True or supplied not in (None, '', '0')
        else:
            value = config.getoption(name) or inherited.get(name) or supplied
        setattr(config.option, name, value)


def qualification_required(config):
    return config.getoption('install_qualification') or os.environ.get('ACC_INSTALL_QUALIFICATION', '') not in ('', '0')


def unavailable(config, reason):
    if qualification_required(config):
        pytest.fail(f'Install qualification requires {reason}; refusing to skip.', pytrace=False)
    pytest.skip(f'{reason} absent; no replacement is installed')


def host_node_tar(config):
    node = shutil.which('node', path=artifact.TRUSTED_PATH)
    if node is None:
        unavailable(config, 'host Node.js/node-tar')
    npm = shutil.which('npm', path=artifact.TRUSTED_PATH)
    candidates = ['tar']
    if npm:
        candidates.insert(0, str(Path(npm).resolve().parent.parent / 'node_modules/tar'))
    script = '''
const fs = require('node:fs'), path = require('node:path');
for (const candidate of JSON.parse(process.argv[1])) {
  let resolved;
  try { resolved = require.resolve(candidate); }
  catch (error) { if (error.code === 'MODULE_NOT_FOUND') continue; throw error; }
  const tar = require(resolved);
  if (typeof tar.t !== 'function' || typeof tar.x !== 'function') throw new Error('Invalid host node-tar');
  for (let directory = path.dirname(resolved); ; directory = path.dirname(directory)) {
    const file = path.join(directory, 'package.json');
    if (fs.existsSync(file)) {
      const pkg = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (pkg.name === 'tar' && typeof pkg.version === 'string') {
        console.log(JSON.stringify({node:process.execPath,node_version:process.version,
          module:fs.realpathSync(resolved),package_json:fs.realpathSync(file),version:pkg.version}));
        process.exit(0);
      }
    }
    if (path.dirname(directory) === directory) throw new Error('node-tar version unavailable');
  }
}
console.log('null');
'''
    result = subprocess.run([node, '-e', script, json.dumps(candidates)], check=True, capture_output=True,
        text=True, env={'PATH': artifact.TRUSTED_PATH, 'LANG': 'C.UTF-8'}, timeout=10)
    identity = json.loads(result.stdout)
    if identity is None:
        unavailable(config, 'host node-tar')
    return identity


def runtime_config(config, role):
    values = {name: config.getoption(f'install_{name}_python') or os.environ.get(f'ACC_INSTALL_{name.upper()}_PYTHON')
              for name in ('accd', 'broker')}
    record = config.getoption('install_qualification_record') or os.environ.get('ACC_INSTALL_QUALIFICATION_RECORD')
    if qualification_required(config) and (not all(values.values()) or not record):
        pytest.fail('Qualification needs explicit accd/broker interpreter paths and a fresh evidence record path.', pytrace=False)
    executable = values[role] or sys.executable
    if not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
        pytest.fail(f'The selected {role} interpreter must be an absolute executable path; no fallback.', pytrace=False)
    return executable, record


def inspect_corpus(executable, requests):
    result = subprocess.run([executable, '-I', str(FIXTURES / 'inspect-corpus.py'), str(Path(artifact.__file__).resolve())],
        input=json.dumps({'cases': requests, 'metadata': {'name': '@reviewed/sample', 'version': '1.2.3', 'bin': 'cli.js'}}),
        capture_output=True, text=True, env={'PATH': artifact.TRUSTED_PATH, 'LANG': 'C.UTF-8'}, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['interpreter']['isolated'] == 1
    return report


def write_record(base, role, evidence):
    if base:
        path = Path(base)
        path = path.with_name(path.stem + '-' + role + (path.suffix or '.json'))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x') as output:
            json.dump(evidence, output, indent=2, sort_keys=True)
            output.write('\n')
        return str(path)
