"""Real Claude CLI / synthetic local model; only run inside probe.sandbox."""
import errno
import fcntl
import hashlib
import http.server
import json
import os
import pty
import socket
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

NATIVE = '11111111-1111-4111-8111-111111111111'


def main():
    assert not Path('/srv/acc').exists()
    assert set(name for _, name in socket.if_nameindex()) == {'lo'}
    assert not any(Path(os.environ['CLAUDE_CONFIG_DIR']).iterdir())
    external = socket.socket()
    assert external.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    external.close()
    run = Path('/home/probe/run')
    run.mkdir(mode=0o700)
    policy = run / 'guardrails.json'
    policy.write_text(json.dumps({'workspace': '/home/probe', 'custom': [{
        'pattern': 'relay-native-forbidden', 'reason': 'Native fixture denies this command.'}]}))
    wrapper = Path('/evidence/guard_fixture.py')
    wrapper.write_text("import runpy,sys; sys.argv=['relay-guard-fixture','claude','PreToolUse']; "
                       "g=runpy.run_path('/evidence/guard_source.py'); raise SystemExit(g['main']())\n")
    settings = run / 'settings.json'
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [{'hooks': [{
        'type': 'command', 'command': '/usr/bin/python3 -I /evidence/guard_fixture.py'}]}]}}))
    tui_mode = Path('/evidence/tui-mode').exists()
    sdk_mode = Path('/evidence/sdk-mode').exists()
    observer_file = run / 'native-hooks.jsonl'
    if tui_mode:
        observer = Path('/evidence/observe_hook.py')
        observer.write_text("import json,sys; from pathlib import Path; p=json.load(sys.stdin); "
            "d={k:p.get(k) for k in ('session_id','hook_event_name')}; "
            "d['fixture_prompt']=p.get('prompt')=='Native TUI isolated fixture turn'; "
            "f=Path('/home/probe/run/native-hooks.jsonl').open('a'); f.write(json.dumps(d)+'\\n'); f.close()\n")
        value = json.loads(settings.read_text())
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd'):
            value['hooks'][event] = [{'hooks': [{'type': 'command', 'command': '/usr/bin/python3 -I /evidence/observe_hook.py'}]}]
        settings.write_text(json.dumps(value))
        # Artificial startup preferences only. No live account/consent is tested.
        preferences = {'hasCompletedOnboarding': True, 'theme': 'dark',
            'projects': {'/home/probe': {'hasTrustDialogAccepted': True}},
            'customApiKeyResponses': {'approved': ['relay-synthetic-fixture-only', 'relay-synthetic-fixture-only'[-20:]], 'rejected': []}}
        for path in (Path('/home/probe/.claude.json'), Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'):
            path.write_text(json.dumps(preferences))
    guard_files = ' '.join(path.name + '=' + hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (policy, settings))
    requests = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            size = int(self.headers.get('content-length', '0'))
            assert size <= 1 << 20
            body = json.loads(self.rfile.read(size))
            # Evidence needs only fixture markers, never bundled CLI instructions
            # or arbitrary model payloads. Preserve roles and observed history.
            markers = ('First isolated fixture turn', 'Second isolated fixture turn', 'Native TUI isolated fixture turn',
                       'Offline fixture reply.', 'Native fixture denies this command.')
            messages = [{'role': item['role'], 'content': [marker for marker in markers
                         if marker in json.dumps(item.get('content', ''))]}
                        for item in body.get('messages', [])]
            requests.append({'path': self.path, 'messages': messages})
            if 'count_tokens' in self.path:
                data = json.dumps({'input_tokens': 10}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            message = {'id': 'msg_fixture_' + str(len(requests)), 'type': 'message', 'role': 'assistant',
                       'model': body.get('model', 'fixture'), 'content': [], 'stop_reason': None,
                       'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 2}}
            events = [
                ('message_start', {'type': 'message_start', 'message': message}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                         'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'text_delta', 'text': 'Offline fixture reply.'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                   'usage': {'output_tokens': 2}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            if len(requests) == 1:
                tool = {'type': 'tool_use', 'id': 'tool_fixture', 'name': 'Bash', 'input': {}}
                events[1] = ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': tool})
                events[2] = ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {
                    'type': 'input_json_delta', 'partial_json': json.dumps({
                        'command': 'printf fixture > /home/probe/relay-native-forbidden',
                        'description': 'Attempt a denied fixture write'})}})
                events[4][1]['delta']['stop_reason'] = 'tool_use'
            data = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n'
                           for name, value in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    env = {**os.environ, 'ANTHROPIC_API_KEY': 'relay-synthetic-fixture-only',
           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:' + str(server.server_port),
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
           'ACC_RUN_DIR': str(run), 'ACC_SESSION_ID': 'fixture', 'ACC_GUARD_FILES': guard_files}
    results = []
    tui_result = None
    def native_tui():
        from types import SimpleNamespace

        from acc.broker.relay_enclosure import Enclosure
        enclosed = Enclosure(Path('/evidence/owners'), SimpleNamespace(run_id='run-tui'), '/usr/bin/claude')
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
        args = ['/usr/bin/python3', '-I', '/evidence/pty_exec.py', str(enclosed.cli), '--resume', NATIVE,
                '--model', 'claude-sonnet-4-5', '--settings', str(settings)]
        before = len(observer_file.read_text().splitlines()) if observer_file.exists() else 0
        child = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
        os.close(slave)
        output = bytearray()
        entered = False
        completed = False
        deadline = time.monotonic() + 18
        try:
            while child.poll() is None and time.monotonic() < deadline and len(output) < 128 << 10:
                try:
                    data = os.read(master, 65536)
                    output.extend(data)
                    if b'\x1b[6n' in data:
                        os.write(master, b'\x1b[1;1R')
                except BlockingIOError:
                    pass
                except OSError:
                    break
                hooks = [json.loads(line) for line in observer_file.read_text().splitlines()[before:]] if observer_file.exists() else []
                ready = any(item.get('hook_event_name') == 'SessionStart' for item in hooks)
                if not entered and ready and time.monotonic() > deadline - 14:
                    os.write(master, b'Native TUI isolated fixture turn')
                    time.sleep(.7)
                    os.write(master, b'\r')
                    entered = True
                hooks = [json.loads(line) for line in observer_file.read_text().splitlines()[before:]] if observer_file.exists() else []
                prompt = next((index for index, item in enumerate(hooks) if item.get('fixture_prompt')), None)
                if prompt is not None and any(item.get('hook_event_name') == 'Stop' for item in hooks[prompt + 1:]):
                    completed = True
                    time.sleep(.3)
                    break
                time.sleep(.03)
        finally:
            if completed and child.poll() is None:
                os.write(master, b'/exit')
                time.sleep(.7)
                os.write(master, b'\r')
                grace = time.monotonic() + 4
                while child.poll() is None and time.monotonic() < grace:
                    try:
                        output.extend(os.read(master, 65536))
                    except (BlockingIOError, OSError):
                        pass
                    time.sleep(.03)
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=8)
            os.close(master)
        started = json.loads((enclosed.path / 'started.json').read_text())
        empty = enclosed.proof('exit.json', f"{started['pid']}:{started['started']}")
        enclosed.close()
        return {'completed': completed, 'entered': entered, 'fixture_preferences_seeded': True, 'exit': child.returncode, 'empty_tree': empty,
                'hooks': hooks, 'output_bytes': len(output), 'terminal_text': output.decode(errors='replace')[-14000:]}
    try:
        for index, prompt in enumerate(('First isolated fixture turn', 'Second isolated fixture turn')):
            args = ['/usr/bin/claude', '-p', '--output-format', 'json', '--model', 'claude-sonnet-4-5',
                    '--settings', str(settings), '--session-id' if index == 0 else '--resume', NATIVE, prompt]
            enclosure = None
            if Path('/evidence/use-enclosure').exists():
                from types import SimpleNamespace

                from acc.broker.relay_enclosure import Enclosure
                enclosure = Enclosure(Path('/evidence/owners'), SimpleNamespace(run_id=f'run{index}'), '/usr/bin/claude')
                args[0] = str(enclosure.cli)
            if sdk_mode:
                import asyncio
                from dataclasses import asdict

                from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
                async def native_sdk():
                    result = None
                    options = ClaudeAgentOptions(cli_path=str(enclosure.cli), cwd='/home/probe',
                        model='claude-sonnet-4-5', env=env, settings=str(settings), permission_mode='default',
                        extra_args={'session-id': NATIVE} if index == 0 else {},
                        resume=NATIVE if index else None, stderr=lambda _line: None)
                    async for message in query(prompt=prompt, options=options):
                        if isinstance(message, ResultMessage):
                            result = asdict(message)
                    assert result is not None
                    return result
                output = asyncio.run(native_sdk())
                result = type('Outcome', (), {'returncode': 0 if not output['is_error'] else 1})()
            else:
                result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=20)
                try:
                    output = json.loads(result.stdout)
                except ValueError:
                    output = {'parse_error': True, 'stdout': result.stdout[:1000], 'stderr': result.stderr[:1000]}
            empty = None
            native_exit_status = None
            if enclosure:
                started = json.loads((enclosure.path / 'started.json').read_text())
                empty = enclosure.proof('exit.json', f"{started['pid']}:{started['started']}")
                native_exit_status = json.loads((enclosure.path / 'exit.json').read_text()).get('status') if empty else None
                enclosure.close()
            results.append({'exit': result.returncode, 'output': output, 'empty_tree': empty, 'native_exit_status': native_exit_status})
            if result.returncode:
                break
            if tui_mode and index == 0:
                tui_result = native_tui()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    import importlib.metadata
    print(json.dumps({'results': results, 'tui': tui_result, 'sdk': sdk_mode,
                      'sdk_version': importlib.metadata.version('claude-agent-sdk') if sdk_mode else None, 'requests': requests,
                      'guard_events': [json.loads(line) for line in (run / 'events.jsonl').read_text().splitlines()]
                          if (run / 'events.jsonl').exists() else [],
                      'forbidden_file_exists': Path('/home/probe/relay-native-forbidden').exists(),
                      'guard_source_digest': 'sha256:' + hashlib.sha256(Path('/evidence/guard_source.py').read_bytes()).hexdigest(),
                      'scope': ('isolated native print/TUI/print same-id resume, native hooks and empty-tree receipts; '
                                'synthetic API/startup preferences; not production hub ownership or full-editor draft attestation') if tui_mode else
                               'isolated native CLI guard and persistence/resume; not TUI or hub ownership'}))


if __name__ == '__main__':
    main()
