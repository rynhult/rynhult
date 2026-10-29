"""Opt-in installed Codex checks against a loopback model and disposable team spool."""
import argparse
import json
import os
import runpy
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def check(tmp_path, *, baseline=False):
    from acc.broker import ops
    from acc.broker.config import BrokerConfig
    from acc.server.teams import team_files

    codex = shutil.which('codex')
    assert codex, 'installed Codex required'
    root = tmp_path / 'repo'
    work = tmp_path / 'helper'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)
    git('init', '-q', '-b', 'main')
    (root / 'README.md').write_text('Scratch only.\n')
    git('add', '.')
    git('-c', 'user.name=Scratch', '-c', 'user.email=scratch@example.com', 'commit', '-qm', 'Initial')
    git('branch', 'lead')
    git('worktree', 'add', '-qb', 'acc/helper/work', str(work))
    git('pack-refs', '--all')
    protected = [root / '.git/hooks/pre-commit', root / '.git/config',
                 root / '.git/info/exclude', root / '.git/packed-refs',
                 root / '.git/refs/heads/main', root / '.git/refs/heads/lead',
                 root / '.git/logs/refs/heads/main', root / '.git/logs/refs/heads/lead']
    original = {str(path): path.read_bytes() if path.exists() else None for path in protected}
    run = tmp_path / 'run'
    (run / 'team').mkdir(parents=True)
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    os.environ['ACC_ORCHESTRATOR'] = '1'
    os.environ['ACC_ORCHESTRATOR_V2'] = '1'
    helper_bin = tmp_path / 'bin'
    helper_bin.mkdir()
    shutil.copyfile(REPO / 'bin/acc-mcp-team', helper_bin / 'acc-mcp-team')
    (helper_bin / 'acc-mcp-team').chmod(0o700)
    shutil.copyfile(REPO / 'src/acc/env.py', helper_bin / 'acc_env.py')
    files = team_files({'codex-overrides.json': '[]', 'guardrails.json': '{"rules":{}}'}, 'codex', str(helper_bin / 'acc-hook'))
    overrides = json.loads(files['codex-overrides.json'])
    replay_api = runpy.run_path(str(REPO / 'scripts/container_qualification/replay.py'))
    requests = []
    actions = []
    stopping = threading.Event()

    def answer():
        seen = set()
        while not stopping.wait(.01):
            spool = run / 'events.jsonl'
            if not spool.exists():
                continue
            for line in spool.read_text().splitlines():
                request = json.loads(line)['hook']['request']
                if request['id'] in seen:
                    continue
                seen.add(request['id'])
                actions.append(request['action'])
                (run / 'team' / (request['id'] + '.json')).write_text(json.dumps({
                    'ok': True, 'result': {'qualification': request['action']}}))
    @contextmanager
    def answering():
        responder = threading.Thread(target=answer)
        responder.start()
        try:
            yield
        finally:
            stopping.set()
            responder.join(timeout=2)
    command = 'set -e; printf "helper change\\n" > helper.txt; git add helper.txt; git -c user.name=Scratch -c user.email=scratch@example.com commit -m Helper'
    command += "; helper_commit=$(git rev-parse HEAD); git update-ref refs/heads/acc/helper/work HEAD^; git update-ref -m 'Qualification branch update' refs/heads/acc/helper/work \"$helper_commit\""
    command += '; git reflog show -1 --format=%gs acc/helper/work'
    for branch in ('main', 'lead'):
        command += f'; if git update-ref refs/heads/{branch} HEAD 2>/dev/null; then printf REF_{branch}_WRITABLE; else printf REF_{branch}_BLOCKED; fi'

    for index, path in enumerate(protected):
        command += '; if printf \"# scratch probe\\n\" >> ' + shlex.quote(str(path))
        command += f' 2>/dev/null; then printf PROTECTED_{index}_WRITABLE; else printf PROTECTED_{index}_BLOCKED; fi'
    command += '; if printf forbidden > ' + shlex.quote(str(root / 'forbidden.txt'))
    command += ' 2>/dev/null; then printf SCOPE_LEAK; else printf SCOPE_BLOCKED; fi'
    with answering(), replay_api['Replay']([command]) as replay:
        handler = replay.server.RequestHandlerClass
        def capture(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            requests.append(json.loads(data))
            index = len(requests) - 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            response = {'id': f'resp_{index}', 'object': 'response', 'status': 'in_progress', 'output': []}
            self.event('response.created', {'type': 'response.created', 'response': response})
            if index == 0:
                code = 'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__acc_team__")));'
                code += 'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));'
                code += 'text(await tools.mcp__acc_team__plan({}));'
                code += 'text(await tools.mcp__acc_team__report(' + json.dumps({
                    'status': 'partial' if baseline else 'done', 'summary': 'Scratch qualification'}) + '));' 
                item = {'id': 'call_qualification', 'type': 'custom_tool_call', 'call_id': 'qualification',
                        'name': 'exec', 'namespace': 'functions', 'input': code, 'status': 'completed'}
            else:
                item = {'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'Qualification complete.', 'annotations': []}]}
            for kind in ('added', 'done'):
                self.event('response.output_item.' + kind, {'type': 'response.output_item.' + kind,
                           'output_index': 0, 'item': item})
            self.event('response.completed', {'type': 'response.completed', 'response': response | {
                'status': 'completed', 'output': [item], 'usage': {'input_tokens': 100, 'output_tokens': 20,
                                                               'total_tokens': 120}}})
        handler.do_POST = capture
        config = ['model_provider="scratch"',
                  f'model_providers.scratch={{name="Scratch",base_url="{replay.url}/v1",wire_api="responses",requires_openai_auth=false}}',
                  'sandbox_workspace_write.network_access=true',
                  'sandbox_workspace_write.exclude_slash_tmp=true',
                  'sandbox_workspace_write.exclude_tmpdir_env_var=true', 'features.shell_snapshot=false', *overrides]
        argv = [codex, 'exec', '--strict-config', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--json',
                '--model', 'gpt-5.6-luna', '--sandbox', 'workspace-write', '-c', 'approval_policy="on-request"']
        for setting in config:
            argv += ['-c', setting]
        if not baseline:
            argv += ops._codex_team_git_args(BrokerConfig(allowed_roots=(tmp_path,)), work,
                                             ['--sandbox', 'workspace-write'], files)
        argv += ['-C', str(work), 'Execute the scratch qualification.']
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
                       'TMPDIR': str(temporary), 'ACC_RUN_DIR': str(run), 'ACC_SESSION_ID': 'scratch-helper'}
        child = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
            raise RuntimeError('Scratch Codex timed out: ' + stderr[-1000:]) from None
        result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    artifact = REPO / '.acc/artifacts' / ('codex-team-baseline.json' if baseline else 'codex-team-real.json')
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                                    'requests': requests, 'actions': actions}, indent=2))
    assert result.returncode == 0, result.stderr
    assert len(requests) >= 2, result.stdout
    tools = '\n'.join(content.get('text', '') for item in requests[-1]['input']
                      if item['type'] == 'custom_tool_call_output' for content in item.get('output', []))
    assert 'mcp__acc_team__report' in tools and 'mcp__acc_team__plan' in tools, result.stderr
    assert actions == ['plan', 'report'], result.stdout
    completed = [event['item'] for line in result.stdout.splitlines()
                 if (event := json.loads(line)).get('type') == 'item.completed'
                 and event['item'].get('type') == 'mcp_tool_call']
    assert [item['tool'] for item in completed] == ['plan', 'report']
    assert all(item['status'] == 'completed' and item['error'] is None for item in completed)
    if not baseline:
        for index, path in enumerate(protected):
            assert f'PROTECTED_{index}_BLOCKED' in tools
            assert (path.read_bytes() if path.exists() else None) == original[str(path)]
    subject = git('log', 'acc/helper/work', '-1', '--format=%s').stdout.strip()
    if baseline:
        assert subject == 'Initial' and 'Read-only file system' in tools
    else:
        assert subject == 'Helper', result.stdout
        for branch in ('main', 'lead'):
            assert f'REF_{branch}_BLOCKED' in tools
            assert git('log', branch, '-1', '--format=%s').stdout.strip() == 'Initial'
        assert git('reflog', 'show', '-1', '--format=%gs', 'acc/helper/work').stdout.strip() == 'Qualification branch update'
        assert not (root / 'forbidden.txt').exists() and 'SCOPE_BLOCKED' in tools and 'SCOPE_LEAK' not in tools


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true', help='Confirm the original sandbox refusal')
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='acc-codex-team-') as temporary:
        isolated = {'PATH': '/usr/bin:/bin', 'HOME': str(Path(temporary) / 'home'),
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'ACC_TEST': '1'}
        with patch.dict(os.environ, isolated, clear=True):
            check(Path(temporary), baseline=options.baseline)
    print('Codex team qualification passed')
