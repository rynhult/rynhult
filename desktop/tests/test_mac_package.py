"""Portable installer script generation; no pkgbuild or account mutation here."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def builder():
    source = ROOT / 'desktop/build_mac_setup_pkg.py'
    assert source.exists(), 'one-time macOS Installer builder missing'
    spec = importlib.util.spec_from_file_location('mac_package_builder', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_dry_run_generates_bound_postinstall_without_writing(tmp_path):
    builder()
    result = subprocess.run([sys.executable, str(ROOT / 'desktop/build_mac_setup_pkg.py'),
        '--owner','alex','--owner-uid','501','--owner-gid','20','--agent-id','50001',
        '--runtime',str(tmp_path/'native-runtime'),'--out',str(tmp_path/'setup.pkg')],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['build'] is False and not list(tmp_path.iterdir())
    script = value['postinstall']
    syntax = subprocess.run(['/bin/sh','-n'],input=script,text=True,capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    assert '--owner alex' in script and '--owner-uid 501' in script and '--agent-id 50001' in script
    assert '--apply' in script and 'mac_setup.py' in script
    assert 'AuthorizationExecuteWithPrivileges' not in script and 'sudo' not in script
    assert value['pkgbuild'][0] == '/usr/bin/pkgbuild'
    assert value['pkgbuild'][value['pkgbuild'].index('--ownership')+1] == 'recommended'
    assert value['installed_resources'] == '/Library/Application Support/Rynhult Desktop/Runtime'


def test_postinstall_refuses_another_target_volume_before_provisioning(tmp_path):
    module = builder()
    value = module.build_plan('alex',501,20,50001,tmp_path/'runtime',tmp_path/'out.pkg','0.0.0')
    script = tmp_path/'postinstall'
    script.write_text(value['postinstall'])
    result = subprocess.run(['/bin/sh',str(script),'package','location','/Volumes/Other'],capture_output=True,text=True)
    assert result.returncode != 0 and 'volume' in result.stderr


def test_smoke_defaults_to_plan_and_never_asks_for_privileges(tmp_path):
    script = ROOT/'desktop/tests/mac-phase1-smoke.sh'
    assert script.is_file(), 'coordinator macOS smoke script missing'
    result = subprocess.run(['/bin/bash',str(script),'--python',sys.executable,'--owner','alex',
        '--owner-uid','501','--owner-gid','20','--agent-id','50001'],cwd=tmp_path,
        input='',capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['apply'] is False and value['agent'] == 'rynd-501'
    assert not list(tmp_path.iterdir())


def test_smoke_refuses_apply_on_linux_before_any_prompt_or_command(tmp_path):
    script = ROOT/'desktop/tests/mac-phase1-smoke.sh'
    assert script.is_file(), 'coordinator macOS smoke script missing'
    if sys.platform != 'darwin':
        result = subprocess.run(['/bin/bash',str(script),'--install','--python',sys.executable,'--owner','alex',
            '--owner-uid','501','--owner-gid','20'],cwd=tmp_path,input='CREATE ry nd\n',capture_output=True,text=True)
        assert result.returncode != 0 and 'macOS' in result.stderr
        assert not list(tmp_path.iterdir())


def test_python_helper_wrapper_handles_spaces_without_using_system_python(tmp_path):
    module = builder()
    resources = tmp_path/'Application Support/Runtime'
    executable = resources/'desktop/runtime/python/bin/python3'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    wrapper = tmp_path/'dummy-helper'
    wrapper.write_text(module.python_wrapper(resources,'dummy-helper'))
    result = subprocess.run(['/bin/sh',str(wrapper),'argument with spaces','$(no-command)'],capture_output=True,text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['-I',str(resources/'bin/dummy-helper.py'),'argument with spaces','$(no-command)']


def test_uninstall_package_has_no_payload_and_requires_explicit_purge():
    module = builder()
    value = module.build_plan('alex',501,20,50001,Path('/tmp/runtime'),Path('/tmp/uninstall.pkg'),'0.0.0',
                              action='uninstall',purge=True,remove_runtime=True)
    assert value['action'] == 'uninstall'
    assert '--purge-agent --confirm-agent rynd-501' in value['postinstall']
    assert '--remove-runtime' in value['postinstall']
    assert value['payload'] is False


def test_package_recipe_verification_refuses_mismatched_or_modified_actions(tmp_path):
    module = builder()
    value = module.build_plan('alex',501,20,50001,tmp_path/'runtime',tmp_path/'out.pkg','0.0.0')
    scripts = tmp_path/'Scripts'
    scripts.mkdir()
    (scripts/'postinstall').write_text(value['postinstall'])
    (scripts/'preinstall').write_text(value['preinstall'])
    (scripts/'mac-plan.json').write_text(json.dumps(module.recipe(value)))
    module.verify_expanded(tmp_path,value)
    (scripts/'postinstall').write_text(value['postinstall']+'echo changed\n')
    try:
        module.verify_expanded(tmp_path,value)
    except ValueError:
        pass
    else:
        raise AssertionError('modified postinstall was accepted')


def test_smoke_cancel_stops_before_opening_installer(tmp_path):
    import os
    from types import SimpleNamespace
    tools = tmp_path/'tools'
    tools.mkdir()
    uname = tools/'uname'
    uname.write_text('#!/bin/sh\necho Darwin\n')
    uname.chmod(0o755)
    package = tmp_path/'reviewed.pkg'
    package.write_text('not opened')
    # Model the ordinary Mac owner even when the gate itself runs as root.
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\ncase "$1" in -un) echo alex;; -u) echo 501;; -g) echo 20;; *) exit 1;; esac\n')
    identity.chmod(0o755)
    who = SimpleNamespace(pw_name='alex', pw_uid=501, pw_gid=20)
    result = subprocess.run(['/bin/bash',str(ROOT/'desktop/tests/mac-phase1-smoke.sh'),
        '--install','--package',str(package),'--python',sys.executable,'--owner',who.pw_name,
        '--owner-uid',str(who.pw_uid),'--owner-gid',str(who.pw_gid)],
        input='no\n',capture_output=True,text=True,env={**os.environ,'PATH':str(tools)+':/usr/bin:/bin'})
    assert result.returncode != 0 and 'Cancelled' in result.stderr
    assert f'CREATE rynd-{who.pw_uid}' in result.stderr


def test_preinstall_refuses_existing_identity_before_runtime_payload(tmp_path):
    module = builder()
    receipt = tmp_path/'Config/501/identity.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{}')
    script = tmp_path/'preinstall'
    script.write_text(module.preinstall_script('install',tmp_path))
    result = subprocess.run(['/bin/sh',str(script),'pkg','location','/'],capture_output=True,text=True)
    assert result.returncode != 0 and 'receipt' in result.stderr
    assert receipt.read_text() == '{}'


def test_smoke_cleans_private_package_after_verification_failure(tmp_path):
    import os
    from types import SimpleNamespace
    tools = tmp_path/'tools'
    tools.mkdir()
    uname = tools/'uname'
    uname.write_text('#!/bin/sh\necho Darwin\n')
    uname.chmod(0o755)
    package = tmp_path/'reviewed.pkg'
    package.write_text('not opened')
    scratch = tmp_path/'scratch'
    scratch.mkdir()
    # Model the ordinary Mac owner even when the gate itself runs as root.
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\ncase "$1" in -un) echo alex;; -u) echo 501;; -g) echo 20;; *) exit 1;; esac\n')
    identity.chmod(0o755)
    who = SimpleNamespace(pw_name='alex', pw_uid=501, pw_gid=20)
    result = subprocess.run(['/bin/bash',str(ROOT/'desktop/tests/mac-phase1-smoke.sh'),
        '--install','--package',str(package),'--python',sys.executable,'--owner',who.pw_name,
        '--owner-uid',str(who.pw_uid),'--owner-gid',str(who.pw_gid)],
        input=f'CREATE rynd-{who.pw_uid}\n',capture_output=True,text=True,
        env={**os.environ,'TMPDIR':str(scratch),'PATH':str(tools)+':/usr/bin:/bin'})
    assert result.returncode != 0 and 'macOS pkgutil' in result.stderr
    assert not list(scratch.iterdir())


def test_execution_payload_contains_production_modules_and_license_notices(tmp_path, monkeypatch):
    module = builder()
    source = tmp_path / 'source'
    files = [*(f'desktop/{name}' for name in module.MODULES), 'desktop/web-dist/index.html',
             'src/acc/__init__.py', 'src/acc/env.py', 'src/acc/tooling.py', 'catalog/manifest.toml',
             'docs/wiki/Guide.md', 'deploy/lib/acc-env.sh', 'LICENSE', 'THIRD_PARTY_NOTICES.md']
    for name in files:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (source / 'bin').mkdir()
    (source / 'bin/acc-dummy').write_text('#!/usr/bin/env python3\nprint("fixture")\n')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'native-runtime-marker').write_text('fixture only')
    monkeypatch.setattr(module, 'ROOT', source)
    destination = tmp_path / 'payload'
    module.stage_resources(destination, runtime)
    for name in module.MODULES:
        assert (destination / 'desktop' / name).is_file()
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        assert (destination / name).read_text() == name
    assert (destination / 'bin/acc-dummy.py').is_file()
    assert '-I' in (destination / 'bin/acc-dummy').read_text()
    assert not (destination / 'desktop/tests').exists()
