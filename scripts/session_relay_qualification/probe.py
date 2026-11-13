#!/usr/bin/env python3
"""No-account native metadata probe inside an enforced, offline mount/network namespace.

The CLI entry point runs fixed metadata commands only. Test helpers run fixed
synthetic native fixtures in the same namespace, with no live account or external
inference. No caller-supplied native command or environment is accepted.
"""
import argparse
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path


class QualificationPinMismatch(RuntimeError):
    """Required native artifact unavailable; never qualify or try another build."""


CODEX_VERSION = 'codex-cli 0.167.1'
CODEX_NATIVE_DIGEST = 'sha256:dd5bfdf19d794dcc6fb482d2d5a2d9fb31d11f6caa44b303bb72890d461e8a02'


def sandbox(output):
    argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
    for name in ('usr', 'bin', 'lib', 'lib64'):
        path = Path('/') / name
        if path.exists():
            argv += ['--ro-bind', str(path), str(path)]
    return argv + ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                   '--tmpfs', '/home', '--dir', '/home/probe', '--dir', '/home/probe/codex',
                   '--dir', '/home/probe/claude', '--bind', str(output), '/evidence',
                   '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                   '--setenv', 'HOME', '/home/probe', '--setenv', 'CODEX_HOME', '/home/probe/codex',
                   '--setenv', 'CLAUDE_CONFIG_DIR', '/home/probe/claude', '--chdir', '/home/probe', '--']


def run(output, command):
    # Deliberately omit every inherited credential and broker/socket variable.
    try:
        result = subprocess.run(sandbox(output) + command, env={'PATH': '/usr/bin:/bin'},
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=True)
    except subprocess.CalledProcessError as error:
        # Only the clearenv offline namespace's diagnostic, never host env.
        error.add_note(error.stderr.decode(errors='replace')[-8192:])
        raise
    return result.stdout.decode().strip()


def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def initialize_stdio(output, executable):
    """Only initialize one private server; never create/resume a conversation."""
    process = subprocess.Popen(sandbox(output) + [str(executable), 'app-server', '--listen', 'stdio://',
                               '-c', 'features.hooks=true'], env={'PATH': '/usr/bin:/bin'},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        request = {'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'rynhult-relay-qualification', 'version': '0'}}}
        process.stdin.write((json.dumps(request) + '\n').encode())
        process.stdin.flush()
        deadline, buffer = time.monotonic() + 15, b''
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), (1 << 20) + 1)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 1 << 20:
                    raise RuntimeError('Native initialization exceeded the frame bound')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    answer = json.loads(line)
                    if answer.get('id') == 1:
                        if not isinstance(answer.get('result'), dict):
                            raise RuntimeError('Native initialization was refused')
                        process.stdin.write(b'{"method":"initialized","params":{}}\n')
                        process.stdin.close()
                        process.wait(timeout=5)
                        if process.returncode:
                            raise RuntimeError('Native server did not exit cleanly')
                        return {'acknowledged': True, 'result_fields': sorted(answer['result'])}
        raise RuntimeError('Native isolated stdio initialization was not acknowledged')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()



def capture_tui():
    """Discover pinned native TUI wire shape against synthetic, offline replies.

    This is evidence of requests only: the synthetic server cannot qualify native
    acceptance, input processing or same-thread execution ownership.
    """
    import shutil
    with tempfile.TemporaryDirectory(prefix='relay-native-tui-') as name:
        output = Path(name)
        output.chmod(0o700)
        if run(output, ['/usr/bin/codex', '--version']) != CODEX_VERSION:
            raise RuntimeError('Native TUI differs from qualification pin')
        shutil.copyfile(Path(__file__).with_name('tui_capture.py'), output / 'driver.py')
        result = json.loads(run(output, ['/usr/bin/python3', '/evidence/driver.py']))
        result['qualification'] = 'wire discovery only; no processed watermark or actor proof'
        return result



def capture_claude_resume(*, enclosure=False, tui=False, sdk=False, draft=False, joint=None, placement=None, fixture_root=None):
    """Fixed native CLI/SDK/TUI modes, synthetic local model and reviewed guard code.

    The local HTTP fixture exists only inside the enforced offline namespace.
    No installed/live hook command or inherited account is invoked.
    """
    import shutil
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='relay-native-claude-') as name:
        output = Path(name)
        output.chmod(0o700)
        cli_version = run(output, ['/usr/bin/claude', '--version'])
        if cli_version != '2.1.308 (Claude Code)':
            raise RuntimeError('Installed Claude version differs from qualification pin')
        cli_digest = digest(Path('/usr/bin/claude'))
        if cli_digest != 'sha256:356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58':
            raise RuntimeError('Installed Claude executable differs from qualification pin')
        for source, target in ((Path(__file__).with_name('claude_resume.py'), 'driver.py'),
                               (root / 'bin/acc-guard', 'guard_source.py'),
                               (root / 'src/acc/env.py', 'acc_env.py')):
            shutil.copyfile(source, output / target)
            (output / target).chmod(0o600)
        installed = output / 'installed'
        installed.mkdir(mode=0o700)
        for source, target in ((root / 'bin/acc-hook', 'relay-hook'), (root / 'bin/acc-guard', 'acc-guard'),
                               (root / 'bin/acc-statusline', 'relay-statusline'), (root / 'deploy/lib/acc-env.sh', 'acc-env.sh'),
                               (root / 'src/acc/env.py', 'acc_env.py')):
            shutil.copyfile(source, installed / target)
            (installed / target).chmod(0o500)
        if placement is not None:
            if not tui or not enclosure or draft or sdk or joint is not None:
                raise ValueError('Placement proof requires enclosed native TUI only')
            shutil.copyfile(Path(__file__).with_name('claude_placement_fixture.py'), output / 'claude_placement_fixture.py')
            driver = output / 'driver.py'
            source = driver.read_text()
            marker = '    def native_tui():\n'
            assert source.count(marker) == 1
            source = source.replace(marker, marker +
                '        from claude_placement_fixture import native_tui as placed_tui\n'
                '        return placed_tui(settings, env, observer_file, requests, NATIVE)\n')
            record = "            requests.append({'path': self.path, 'messages': messages})"
            assert source.count(record) == 1
            source = source.replace(record, record + "\n            requests[-1]['current_fixture_markers'] = [m for m in markers if m in json.dumps(body.get('messages', [{}])[-1].get('content', ''))]; requests[-1]['model'] = body.get('model'); requests[-1]['max_tokens'] = body.get('max_tokens'); requests[-1]['tool_count'] = len(body.get('tools', []))")
            native_tool = "            if len(requests) == 1:\n"
            assert source.count(native_tool) == 1
            source = source.replace(native_tool,
                "            native_permission = (Path('/evidence/request-native-permission').exists() and body.get('tools')\n"
                "                and ('Native TUI isolated fixture turn' in json.dumps(body.get('messages', [{}])[-1].get('content', '')) or\n"
                "                     Path('/evidence/request-native-public-permission').exists()))\n"
                "            native_background = (Path('/evidence/request-native-background').exists() and body.get('tools')\n"
                "                and 'Native TUI isolated fixture turn' in json.dumps(body.get('messages', [{}])[-1].get('content', '')))\n"
                "            if native_permission:\n"
                "                Path('/evidence/request-native-permission').unlink()\n"
                "            if native_background:\n"
                "                Path('/evidence/request-native-background').unlink()\n"
                "            if len(requests) == 1 or native_permission or native_background:\n")
            serialize = "            data = ''.join('event: ' + name"
            assert source.count(serialize) == 1
            source = source.replace(serialize,
                "            if native_permission:\n"
                "                events[1][1]['content_block']['id'] = 'native_permission_fixture'\n"
                "                events[2][1]['delta']['partial_json'] = json.dumps({'command': 'printf fixture > ' + __import__('shlex').quote(json.loads(Path('/evidence/placement-config.json').read_text())['workspace'] + '/relay-native-permission-forbidden'), 'description': 'Deny a harmless native fixture marker write'})\n"
                "            if native_permission and Path('/evidence/request-native-guard').exists():\n"
                "                Path('/evidence/request-native-guard').unlink()\n"
                "                work = Path(json.loads(Path('/evidence/placement-config.json').read_text())['workspace'])\n"
                "                events[2][1]['delta']['partial_json'] = json.dumps({'command': 'cat ' + __import__('shlex').quote(str(work / '.env')) + ' > ' + __import__('shlex').quote(str(work / 'relay-native-guard-forbidden')), 'description': 'Synthetic forbidden-path guard proof'})\n"
                "            if native_background:\n"
                "                bash = next(tool for tool in body['tools'] if tool['name'] == 'Bash')\n"
                "                assert 'run_in_background' in bash['input_schema']['properties']\n"
                "                work = Path(json.loads(Path('/evidence/placement-config.json').read_text())['workspace'])\n"
                "                quote = __import__('shlex').quote\n"
                "                command = 'printf started > ' + quote(str(work / 'native-background-started')) + '\\nwhile [ ! -f ' + quote(str(work / 'release-native-background')) + ' ]; do sleep .1; done\\nprintf done > ' + quote(str(work / 'native-background-finished'))\n"
                "                events[1][1]['content_block']['id'] = 'native_background_fixture'\n"
                "                events[2][1]['delta']['partial_json'] = json.dumps({'command': command, 'run_in_background': True, 'description': 'Wait for offline fixture background release'})\n" + serialize)
            # Hold only the real native user-turn model reply, never a hook or
            # owner result. Private inspection can release this offline barrier.
            response = "            message = {'id': 'msg_fixture_' + str(len(requests))"
            assert source.count(response) == 1
            source = source.replace(response,
                "            if (Path('/evidence/hold-model').exists() and body.get('tools')\n"
                "                    and ('Native TUI isolated fixture turn' in json.dumps(body.get('messages', [{}])[-1].get('content', '')) or Path('/evidence/public-hold-model').exists())):\n"
                "                Path('/evidence/model-waiting').touch()\n"
                "                deadline = time.monotonic() + (60 if Path('/evidence/long-hold-model').exists() else 25)\n"
                "                while Path('/evidence/hold-model').exists():\n"
                "                    assert time.monotonic() < deadline, 'Offline model barrier timed out'\n"
                "                    time.sleep(.01)\n" + response)
            # Only trusted source is mounted under /srv/acc, never account data.
            source = source.replace("assert not Path('/srv/acc').exists()", "assert not Path('/srv/acc/data').exists()")
            driver.write_text(source)
        if joint is not None:
            if not tui or not enclosure or draft or sdk:
                raise ValueError("Joint proof requires only enclosed native TUI mode")
            shutil.copyfile(Path(__file__).with_name("claude_joint_fixture.py"), output / "claude_joint_fixture.py")
            driver = output / "driver.py"
            source = driver.read_text()
            marker = "    def native_tui():\n"
            assert source.count(marker) == 1
            driver.write_text(source.replace(marker, marker +
                "        from claude_joint_fixture import native_tui as joint_tui\n"
                "        return joint_tui(settings, env, observer_file, requests, NATIVE)\n"))
        if draft:
            if not tui or not enclosure:
                raise ValueError('Draft proof requires enclosed native TUI')
            shutil.copyfile(Path(__file__).with_name('claude_draft_fixture.py'), output / 'claude_draft_fixture.py')
            driver = output / 'driver.py'
            source = driver.read_text()
            marker = '    def native_tui():\n'
            assert source.count(marker) == 1
            source = source.replace(marker, marker +
                '        from claude_draft_fixture import native_tui as draft_tui\n'
                '        return draft_tui(settings, env, observer_file, requests, NATIVE)\n')
            # Extend the private fixture hook, not the installed/native client.
            hook = 'import json,sys; from pathlib import Path; p=json.load(sys.stdin); '
            assert source.count(hook) == 1
            source = source.replace(hook, 'import json,sys,hashlib; from pathlib import Path; p=json.load(sys.stdin); ' +
                "d_digest='sha256:'+hashlib.sha256(p.get('prompt','').encode()).hexdigest(); ")
            record = "d['fixture_prompt']=p.get('prompt')=='Native TUI isolated fixture turn'; "
            assert source.count(record) == 1
            source = source.replace(record, record + "d['prompt_digest']=d_digest; ")
            driver.write_text(source)
        if sdk:
            if not tui or not enclosure:
                raise ValueError('SDK/TUI proof requires the enclosed native TUI fixture')
            import importlib.metadata
            import importlib.util
            if importlib.metadata.version('claude-agent-sdk') != '0.1.50':
                raise RuntimeError('Installed Claude SDK differs from qualification pin')
            (output / 'sdk-mode').touch()
            distributions = importlib.metadata.packages_distributions()
            for name in ('claude_agent_sdk', 'mcp', 'mcp_types', 'httpx2', 'python_multipart', 'watchfiles',
                         'cryptography', '_cffi_backend', 'opentelemetry', 'anyio', 'sniffio', 'typing_extensions', 'httpx', 'httpcore',
                         'certifi', 'h11', 'pydantic', 'pydantic_core', 'annotated_types', 'typing_inspection',
                         'idna', 'httpx_sse', 'sse_starlette', 'starlette', 'uvicorn', 'dotenv', 'click',
                         'jsonschema', 'jsonschema_specifications', 'referencing', 'rpds', 'attrs', 'attr'):
                spec = importlib.util.find_spec(name)
                if spec is None:
                    continue
                if spec.submodule_search_locations:
                    shutil.copytree(Path(spec.origin).parent if spec.origin else Path(next(iter(spec.submodule_search_locations))), output / name,
                        ignore=shutil.ignore_patterns('__pycache__', '_bundled'))
                else:
                    shutil.copyfile(spec.origin, output / Path(spec.origin).name)
                for distribution_name in distributions.get(name, []):
                    distribution = importlib.metadata.distribution(distribution_name)
                    metadata = output / distribution._path.name
                    metadata.mkdir(exist_ok=True)
                    for filename in ('METADATA', 'WHEEL', 'top_level.txt', 'entry_points.txt'):
                        source = distribution._path / filename
                        if source.is_file():
                            shutil.copyfile(source, metadata / filename)
        if tui:
            if not enclosure:
                raise ValueError('Native TUI proof requires enclosure')
            (output / 'tui-mode').touch()
            shutil.copyfile(root / 'src/acc/broker/pty_exec.py', output / 'pty_exec.py')
        if enclosure:
            package = output / 'acc'
            package.mkdir()
            (package / '__init__.py').write_text('')
            shutil.copyfile(root / 'src/acc/peercred.py', package / 'peercred.py')
            shutil.copytree(root / 'src/acc/session_relay', package / 'session_relay', ignore=shutil.ignore_patterns('__pycache__'))
            broker = package / 'broker'
            broker.mkdir()
            (broker / '__init__.py').write_text('')
            for name in ('session_relay.py', 'relay_enclosure.py', 'claude_admission.py'):
                shutil.copyfile(root / 'src/acc/broker' / name, broker / name)
            (output / 'use-enclosure').touch()
        if placement is not None:
            shutil.rmtree(output / 'acc')
            shutil.copytree(root / 'src/acc', output / 'acc', ignore=shutil.ignore_patterns('__pycache__'))
            import pwd
            import sys
            account = pwd.getpwuid(os.getuid())
            (output / 'passwd').write_text(f'{account.pw_name}:x:{os.getuid()}:{os.getgid()}:offline fixture:/home/probe:/bin/sh\n')
            cleanup = placement(output)
            try:
                config = json.loads((output / 'placement-config.json').read_text())
                argv = sandbox(output)[:-1]
                for fixture in (config['workspace'], config['run_dir'], *([config['main_root']] if config.get('main_root') else [])):
                    path = Path(fixture).resolve(strict=True)
                    if fixture_root is None:
                        allowed = str(path).startswith('/tmp/acc-pytest-')
                    else:
                        base = Path(fixture_root).resolve(strict=True)
                        allowed = str(base).startswith('/tmp/relay-m3-browser-native/') and path.is_relative_to(base)
                    if not allowed:
                        raise ValueError('Placement mounts must be disposable test fixtures')
                    argv += ['--bind', str(path), str(path)]
                venv = Path(sys.prefix).resolve(strict=True)
                if venv == Path('/usr'):
                    raise ValueError('Use the existing project SDK interpreter')
                argv += ['--ro-bind', str(output / 'installed'), '/evidence/installed',
                         '--ro-bind', str(venv), str(venv), '--ro-bind', str(root / 'src'), str(root / 'src'),
                         '--dir', '/etc', '--ro-bind', str(output / 'passwd'), '/etc/passwd',
                         '--setenv', 'PYTHONPATH', str(root / 'src'), '--chdir', config['workspace'], '--',
                         sys.executable, '/evidence/driver.py']
                result = subprocess.run(argv, env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=min(240, int(config.get('timeout', 100)) + 20))
                if result.returncode:
                    raise RuntimeError(result.stderr.decode(errors='replace')[-12000:])
                return {**json.loads(result.stdout), 'cli_version': cli_version, 'cli_digest': cli_digest}
            finally:
                cleanup()
        cleanup = joint(output) if joint is not None else None
        try:
            result = json.loads(run(output, ['/usr/bin/python3', '/evidence/driver.py']))
            return {**result, 'cli_version': cli_version, 'cli_digest': cli_digest}
        finally:
            if cleanup is not None:
                cleanup()


def probe():
    with tempfile.TemporaryDirectory(prefix='relay-native-metadata-') as name:
        output = Path(name)
        output.chmod(0o700)
        # Prove external routing is absent in the same namespace configuration.
        isolation = run(output, ['/usr/bin/python3', '-c',
            'import errno,json,os,socket; s=socket.socket(); s.settimeout(1); '
            'r=s.connect_ex(("192.0.2.1",443)); '
            'assert r == errno.ENETUNREACH; '
            'assert set(n for _,n in socket.if_nameindex()) == {"lo"}; '
            'assert not os.path.exists("/srv/acc"); '
            'assert not os.path.exists("/run/user"); '
            'print(json.dumps({"network_namespace":os.readlink("/proc/self/ns/net"),"egress_errno":r}))'])
        if json.loads(isolation)['network_namespace'] == os.readlink('/proc/self/ns/net'):
            raise RuntimeError('Network namespace was not isolated')
        version = run(output, ['/usr/bin/codex', '--version'])
        if version != CODEX_VERSION:
            raise RuntimeError('Installed Codex version does not match the qualification pin')
        run(output, ['/usr/bin/codex', 'app-server', 'generate-json-schema', '--out', '/evidence/schema'])
        schemas = {str(path.relative_to(output / 'schema')): digest(path)
                   for path in sorted((output / 'schema').rglob('*.json'))}
        if not schemas:
            raise RuntimeError('No native schemas generated')
        wrapper = Path('/usr/bin/codex').resolve(strict=True)
        # Include both npm launcher and packaged native executable, when present.
        binaries = sorted(wrapper.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
        if len(binaries) != 1 or digest(binaries[0]) != CODEX_NATIVE_DIGEST:
            raise RuntimeError('Installed Codex native executable differs from the coordinator qualification pin')
        stdio = initialize_stdio(output, binaries[0])
        claude = run(output, ['/usr/bin/claude', '--version'])
        return {'version': version, 'machine': os.uname().nodename,
                'placement': 'bubblewrap; offline; no accounts; private stdio metadata only',
                'isolation': json.loads(isolation), 'launcher': {'path': str(wrapper), 'digest': digest(wrapper)},
                'native_binaries': [{'path': str(path), 'digest': digest(path)} for path in binaries],
                'claude': {'version': claude, 'digest': digest(Path('/usr/bin/claude'))},
                'stdio_initialize': stdio,
                'schemas': schemas,
                'qualified': ['CLI version and generated schema inventory in isolated metadata placement',
                              'isolated app-server stdio initialize/initialized and clean EOF exit'],
                'unqualified': ['production placement', 'provider admission', 'native TUI correlation',
                                'approvals/config interception', 'guard enforcement', 'same-id handover',
                                'TerminalHost mediation', 'R1 pilot']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(probe(), indent=2, sort_keys=True))


def capture_owned_tui(*, guard=False, guard_tampered=False, writer=False, host=False):
    """Stock TUI with this source's closed proxy and one real private app-server."""
    import shutil

    import websockets
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='relay-owned-tui-') as name:
        output = Path(name)
        output.chmod(0o700)
        observed = run(output, ['/usr/bin/codex', '--version'])
        if observed != CODEX_VERSION:
            raise QualificationPinMismatch(f'Native qualification blocked: expected {CODEX_VERSION}, installed {observed}')
        wrapper = Path('/usr/bin/codex').resolve(strict=True)
        binaries = sorted(wrapper.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
        if len(binaries) != 1 or digest(binaries[0]) != CODEX_NATIVE_DIGEST:
            raise QualificationPinMismatch('Native qualification blocked: installed native digest differs from approved pin')
        package = output / 'acc'
        package.mkdir()
        (package / '__init__.py').write_text('')
        shutil.copyfile(root / 'src/acc/peercred.py', package / 'peercred.py')
        shutil.copytree(root / 'src/acc/session_relay', package / 'session_relay', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(Path(websockets.__file__).parent, output / 'websockets', ignore=shutil.ignore_patterns('__pycache__'))
        driver = 'native_host.py' if host else 'owned_tui.py'
        shutil.copyfile(Path(__file__).with_name(driver), output / 'driver.py')
        if host:
            shutil.copyfile(Path(__file__).with_name('process_cleanup.py'), output / 'process_cleanup.py')
            if guard or writer or guard_tampered:
                raise ValueError('Host proof is a separate fixed fixture')
            broker = package / 'broker'
            broker.mkdir()
            (broker / '__init__.py').write_text('')
            for module in ('attach.py', 'pty_exec.py', 'relay_tui.py', 'relay_observer.py', 'relay_enclosure.py', 'session_relay.py'):
                shutil.copyfile(root / 'src/acc/broker' / module, broker / module)
            shutil.copyfile(root / 'src/acc/python_child.py', package / 'python_child.py')
        if writer:
            if guard:
                raise ValueError('Writer fixture and guard fixture are separate bounded proofs')
            (output / 'writer-mode').touch()
        if guard_tampered and not guard:
            raise ValueError('Tamper fixture requires guard fixture')
        if guard:
            if guard_tampered:
                (output / 'guard-tampered').touch()
            (output / 'guard-mode').touch()
            shutil.copyfile(root / 'bin/acc-guard', output / 'guard_source.py')
            shutil.copyfile(root / 'src/acc/env.py', output / 'acc_env.py')
        (output / 'native-path').write_text(str(binaries[0]))
        result = json.loads(run(output, ['/usr/bin/python3', '/evidence/driver.py']))
        return {**result, 'cli_version': observed, 'cli_digest': digest(binaries[0])}
