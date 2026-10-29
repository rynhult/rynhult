"""Disposable real-Claude plan-mode qualification; loopback model, no account credentials.

Run with PYTHONPATH=src and the project Python, outside pytest's deliberate
ban on real provider processes. Acknowledgements come from a scratch spool responder.
"""
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from container_qualification.replay import Replay

from acc.server.teams import team_files

ROOT = Path(__file__).resolve().parents[1]


def check(fixed, negative="write"):
    with tempfile.TemporaryDirectory(prefix='acc-plan-mcp-') as temp:
        base = Path(temp)
        repo = base/'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True,
                       env={'PATH': '/usr/bin:/bin', 'HOME': str(base), 'GIT_CONFIG_NOSYSTEM': '1',
                            'GIT_CONFIG_GLOBAL': '/dev/null'})
        run = base/'run'
        run.mkdir()
        (run/'team').mkdir()
        binaries = base/'bin'
        binaries.mkdir()
        for name in ('acc-mcp-team', 'acc-guard'):
            shutil.copyfile(ROOT/'bin'/name, binaries/name)
            (binaries/name).chmod(0o700)
        shutil.copyfile(ROOT/'src/acc/env.py', binaries/'acc_env.py')
        policy = run/'guardrails.json'
        policy.write_text(json.dumps({'rules': {'team': True}, 'workspace': str(repo)}))
        settings = {'permissions': {'allow': ['mcp__acc-team__*']}, 'hooks': {
            'PreToolUse': [{'hooks': [{'type': 'command', 'command': f'{binaries}/acc-guard claude PreToolUse'}]}]}}
        if fixed:
            generated = team_files({'settings.json': json.dumps(settings),
                                    'guardrails.json': policy.read_text()},
                                   'claude', str(binaries/'acc-hook'))
            settings = json.loads(generated['settings.json'])
            policy.write_text(generated['guardrails.json'])
        (run/'settings.json').write_text(json.dumps(settings))
        server = {'command': str(binaries/'acc-mcp-team'), 'args': ['--orchestrator', '--orchestrator-v2']}
        (run/'mcp.json').write_text(json.dumps({'mcpServers': {'acc-team': server, 'other': server}}))
        actions = []
        stop = threading.Event()

        def respond():
            seen = set()
            while not stop.wait(0.02):
                path = run/'events.jsonl'
                for line in path.read_text().splitlines() if path.exists() else []:
                    try:
                        event = json.loads(line)['hook']
                        if event.get('hook_event_name') != 'AccTeam':
                            continue
                        request = event['request']
                        if request['id'] in seen:
                            continue
                        seen.add(request['id'])
                        actions.append(request['action'])
                        (run/'team'/f"{request['id']}.json").write_text(json.dumps({'ok': True, 'result': 'acknowledged'}))
                    except (ValueError, KeyError):
                        continue
        responder = threading.Thread(target=respond, daemon=True)
        responder.start()
        sequence = [('mcp__acc-team__report', {'status': 'done', 'summary': 'Scratch qualification'}),
                    ('mcp__acc-team__plan', {}), ('mcp__acc-team__brief', {}),
                    ('Write', {'file_path': str(repo/'forbidden.txt'), 'content': 'must not be written'})]
        if negative == 'bash':
            sequence[-1] = ('Bash', {'command': 'printf forbidden > forbidden.txt'})
        elif negative == 'other':
            sequence[-1] = ('mcp__other__report', {'status': 'done', 'summary': 'Must require approval'})
        with Replay([]) as replay:
            handler = replay.server.RequestHandlerClass

            def post(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/count_tokens'):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens":100}')
                    return
                index = replay.calls
                if not body.get('tools'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.anthropic('', None, 999)
                    return
                replay.calls += 1
                name, args = sequence[index] if index < len(sequence) else ('', None)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                # The shared replay helper treats falsey arguments as end-of-turn;
                # MCP reads legitimately take an empty argument object.
                class Arguments(dict):
                    def __bool__(self):
                        return True
                self.anthropic(name, Arguments(args) if args is not None else None, index)
            handler.do_POST = post
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(base/'home'),
                   'CLAUDE_CONFIG_DIR': str(base/'home/.claude'), 'TERM': 'xterm-256color',
                   'ANTHROPIC_API_KEY': 'qualification-only', 'ANTHROPIC_BASE_URL': replay.url,
                   'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'ENABLE_TOOL_SEARCH': 'false',
                   'ACC_RUN_DIR': str(run), 'ACC_SESSION_ID': 'scratch',
                   'ACC_GUARD_FILES': 'guardrails.json='+hashlib.sha256(policy.read_bytes()).hexdigest()}
            Path(env['CLAUDE_CONFIG_DIR']).mkdir(parents=True)
            # Only onboarding is pre-accepted, never a tool permission.
            (Path(env['CLAUDE_CONFIG_DIR'])/'.credentials.json').write_text('{}')
            (Path(env['CLAUDE_CONFIG_DIR'])/'.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True, 'theme': 'dark',
                'projects': {str(repo): {'hasTrustDialogAccepted': True}},
                'customApiKeyResponses': {'approved': ['qualification-only']}}))
            master, slave = pty.openpty()
            process = subprocess.Popen(['/usr/bin/claude', '--permission-mode', 'plan',
                '--settings', str(run/'settings.json'), '--mcp-config', str(run/'mcp.json'),
                '--strict-mcp-config', '--setting-sources', '', '--model', 'claude-sonnet-4-5',
                '--system-prompt', 'Follow the requested scratch qualification steps.',
                'Call the requested team tools. Stay in plan mode.'],
                cwd=repo, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            output = b''
            answered_trust = False
            deadline = time.monotonic()+45
            try:
                while time.monotonic() < deadline and process.poll() is None:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', ' ', output.decode(errors='replace'))
                    text = ' '.join(text.split())
                    if not answered_trust and ('trust this folder' in text or 'trust the files' in text):
                        os.write(master, b'\x1b[B')
                        time.sleep(0.3)
                        os.write(master, b'\r')
                        answered_trust = True
                    # Stop before answering any tool prompt.
                    events = [json.loads(line) for line in (run/'events.jsonl').read_text().splitlines()] if (run/'events.jsonl').exists() else []
                    guarded_tools = [event['hook'].get('tool_name') for event in events]
                    if (not fixed and 'Do you want to proceed?' in text) or (fixed and sequence[-1][0] in guarded_tools):
                        time.sleep(1)
                        break
                result = {'fixed': fixed, 'negative': negative, 'calls': replay.calls, 'actions': actions,
                          'negative_attempted': sequence[-1][0] in guarded_tools,
                          'permission_modes': sorted({event['hook'].get('permission_mode', '') for event in events if event['hook'].get('tool_name')}),
                          'workspace_write': (repo/'forbidden.txt').exists(),
                          'approval_keystrokes': 0,
                          'permission_prompt': 'Do you want to proceed?' in text}
                # Raw terminal output is private scratch evidence, not printed.
                if not replay.calls:
                    result['startup_tail'] = text[-1800:]
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                os.close(master)
                stop.set()
                responder.join(timeout=2)
        return result


if __name__ == '__main__':
    baseline = check(False)
    fixed = [check(True, negative) for negative in ('write', 'bash', 'other')]
    result = {'baseline': baseline, 'fixed': fixed}
    print(json.dumps(result, indent=2))
    assert baseline['actions'] == [] and baseline['permission_prompt'], baseline
    for run in fixed:
        assert run['actions'] == ['report', 'plan', 'brief'], run
        assert run['negative_attempted'] and not run['workspace_write'], run
        assert run['permission_modes'] == ['plan'], run
