"""Pack an inert, local package offline using the installed npm; no scripts run."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

PACKAGE = {'name': '@reviewed/sample', 'version': '1.2.3', 'bin': 'cli.js'}


def build():
    npm = shutil.which('npm')
    if npm is None:
        raise SystemExit('Installed npm is required; do not install a replacement.')
    with tempfile.TemporaryDirectory(prefix='install-b2-npm-pack-') as directory:
        work = Path(directory)
        source = work / 'source'
        source.mkdir()
        (source / 'package.json').write_text(json.dumps(PACKAGE))
        (source / 'cli.js').write_bytes(b'#!/usr/bin/node\n')
        target = source / ('a' * 120) / ('b' * 120) / ('c' * 87 + '.js')
        target.parent.mkdir(parents=True)
        target.write_bytes(b'long path fixture\n')
        (work / 'home').mkdir()
        for name in ('user.npmrc', 'global.npmrc'):
            (work / name).write_text('')
        command = [npm, 'pack', '--ignore-scripts', '--offline', '--json', '--pack-destination', str(work),
            '--cache', str(work / 'cache'), '--userconfig', str(work / 'user.npmrc'),
            '--globalconfig', str(work / 'global.npmrc')]
        result = subprocess.run(command, cwd=source, env={
            'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'LANG': 'C.UTF-8', 'HOME': str(work / 'home')}, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise SystemExit(result.stderr)
        metadata = json.loads(result.stdout)[0]
        destination = Path(__file__).parent / 'npm-pack-long-path.tgz'
        destination.write_bytes((work / metadata['filename']).read_bytes())
        print(f'{destination.name}: {len(metadata["files"])} files, {destination.stat().st_size} compressed bytes')


if __name__ == '__main__':
    build()
