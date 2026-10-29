"""Release attachment jobs must never publish a hyphenated tag as stable."""
import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ['releases.yml', 'installers-macos.yml', 'installers-windows.yml']


@pytest.mark.parametrize('workflow', WORKFLOWS)
@pytest.mark.parametrize('tag', ['v0.15.0', 'v0.15.0-rc.1', '0.15.0-beta.2', 'v1.0.0', 'v1.0.0-rc.1', '1.0.0-beta.2', 'v2.0.0-preview'])
def test_release_creation_marks_every_hyphenated_tag_prerelease(workflow, tag, tmp_path):
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    scripts = [step['run'] for job in document['jobs'].values() for step in job['steps']
               if 'gh release create' in step.get('run', '')]
    assert len(scripts) == 1
    script = scripts[0]
    assert '--prerelease' in script  # Guard the actual workflow text, not a detached helper.
    calls = tmp_path / 'calls'
    fake = '''gh() {
      printf '%s\\n' "$*" >> "$CALLS"
      if [[ "$1 $2" == "release view" ]]; then return 1; fi
    }
    '''
    subprocess.run(['bash', '-e', '-c', fake + script], check=True,
                   env={**os.environ, 'TAG': tag, 'GITHUB_REPOSITORY': 'example/test', 'CALLS': str(calls)})
    create = [line for line in calls.read_text().splitlines() if line.startswith('release create ')]
    assert len(create) == 1
    assert ('--prerelease' in create[0].split()) == ('-' in tag)
    if '-' in tag:
        assert '--latest=false' in create[0].split()


def test_public_rc_tag_triggers_source_release_but_not_deferred_native_installers():
    from fnmatch import fnmatchcase

    def triggers(workflow, tag):
        document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
        # PyYAML's YAML 1.1 loader reads GitHub's `on` as True.
        event = document.get('on', document.get(True))
        return any(fnmatchcase(tag, pattern) for pattern in event['push']['tags'])

    assert triggers('releases.yml', 'v0.15.0-rc.1')
    assert triggers('releases.yml', 'v0.15.0')
    assert not triggers('releases.yml', 'v0.14.0')
    assert not triggers('installers-macos.yml', 'v0.15.0-rc.1')
    assert not triggers('installers-windows.yml', 'v0.15.0-rc.1')


@pytest.mark.parametrize('signed', [False, True])
def test_source_upload_attaches_an_offline_signature_when_present(tmp_path, signed):
    document = yaml.safe_load((ROOT / '.github/workflows/releases.yml').read_text())
    script = next(step['run'] for step in document['jobs']['source']['steps']
                  if step.get('name') == 'Attach source to release')
    dist = tmp_path / 'dist'
    dist.mkdir()
    if signed:
        (dist / 'SHA256SUMS.asc').write_text('offline signature fixture')
    calls = tmp_path / 'calls'
    fake = 'gh() { printf "%s\\n" "$*" >> "$CALLS"; }\n'
    subprocess.run(['bash', '-e', '-c', fake + script], cwd=tmp_path, check=True,
                   env={**os.environ, 'TAG': 'v0.15.0-rc.1', 'GITHUB_REPOSITORY': 'example/test', 'CALLS': str(calls)})
    upload = next(line for line in calls.read_text().splitlines() if line.startswith('release upload '))
    assert ('dist/SHA256SUMS.asc' in upload.split()) is signed


@pytest.mark.parametrize('tag, accepted', [('v0.15.0-rc.1', True), ('v0.15.0', False)])
def test_source_release_tag_must_match_rc_project_version(tag, accepted, tmp_path):
    import sys

    document = yaml.safe_load((ROOT / '.github/workflows/releases.yml').read_text())
    script = next(step['run'] for step in document['jobs']['source']['steps']
                  if step.get('name') == 'Build exact tagged source and checksum')
    validator = script.split("python3 - <<'PY'\n", 1)[1].split('\nPY', 1)[0]
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "0.15.0-rc.1"\n')
    result = subprocess.run([sys.executable, '-c', validator], cwd=tmp_path,
                            env={**os.environ, 'TAG': tag}, capture_output=True, text=True)
    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize('tag,selected', [
    ('v0.15.0-rc.1', True),
    ('v0.14.0', False),
    ('v0.15.0', True),
    ('v1.0.0', True),
])
def test_source_release_selects_public_rc_and_final_without_enabling_old_preview_releases(tag, selected):
    document = yaml.load((ROOT / '.github/workflows/releases.yml').read_text(), Loader=yaml.BaseLoader)
    patterns = document['on']['push']['tags']
    assert any(fnmatchcase(tag, pattern) for pattern in patterns) is selected
