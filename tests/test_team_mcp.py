"""Exercise the shipped stdio server, including its token-free spool transport."""
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('command', ['acc-mcp-team', 'rynhult-mcp-team'])
def test_mcp_discovery_and_spool_call(tmp_path, command):
    import threading
    run = tmp_path/'run'
    run.mkdir()
    (run/'team').mkdir()
    def answer():
        import time
        for _ in range(200):
            if (run/'events.jsonl').exists():
                record = json.loads((run/'events.jsonl').read_text().splitlines()[0])
                assert record['acc_session'] == 'abc123'
                request = record['hook']['request']
                (run/'team'/f"{request['id']}.json").write_text(json.dumps({'ok': True, 'result': []}))
                return
            time.sleep(.01)
    worker = threading.Thread(target=answer)
    worker.start()
    requests = [{'id': 1, 'method': 'initialize', 'params': {}}, {'id': 2, 'method': 'tools/list'},
                {'id': 3, 'method': 'tools/call', 'params': {'name': 'list', 'arguments': {}}}]
    script = Path(__file__).resolve().parents[1] / 'bin/acc-mcp-team'
    if command.startswith('rynhult'):
        alias = tmp_path / command
        alias.symlink_to(script)
        script = alias
    # As a launch with the orchestrator preview on passes it (teams.team_files); without it, M17's tools only.
    result = subprocess.run([str(script), '--orchestrator'],
                            input='\n'.join(json.dumps(x) for x in requests)+'\n', text=True, capture_output=True,
                            env={**{k: v for k, v in os.environ.items() if '_ORCHESTRATOR' not in k},
                                 'ACC_RUN_DIR': str(run), 'ACC_SESSION_ID': 'abc123'}, timeout=8)
    worker.join()
    assert result.returncode == 0, result.stderr
    replies = [json.loads(x) for x in result.stdout.splitlines()]
    assert {tool['name'] for tool in replies[1]['result']['tools']} == {
        'start', 'list', 'status', 'read', 'message', 'stop', 'finish', 'merge',
        'assign', 'give', 'select', 'selection_status',                                # slice 3
        'report',                                                    # slice 4
        'reserve', 'release', 'gate', 'gate_status', 'restack',      # slice 5
    }
    assert json.loads(replies[2]['result']['content'][0]['text']) == []


def test_document_tools_do_not_mislabel_writes_as_reads(tmp_path):
    import sys
    script = Path(__file__).resolve().parents[1] / 'bin/acc-mcp-team'
    result = subprocess.run([sys.executable, '-I', str(script), '--orchestrator', '--orchestrator-v2'],
        input=json.dumps({'id': 1, 'method': 'tools/list'}) + '\n', text=True, capture_output=True,
        env={'ACC_RUN_DIR': str(tmp_path), 'ACC_SESSION_ID': 'synthetic'}, timeout=8)
    assert result.returncode == 0, result.stderr
    tools = {t['name']: t for t in json.loads(result.stdout)['result']['tools']}
    assert tools['brief']['annotations']['readOnlyHint'] is True
    assert tools['brief_documents']['annotations']['readOnlyHint'] is True
    for name in ('brief_propose_documents', 'brief_share', 'brief_reference', 'report', 'plan_update'):
        assert tools[name]['annotations']['readOnlyHint'] is False, name


def test_guard_read_allow_list_matches_mcp_annotations(tmp_path):
    import sys

    from test_guardrails import guard
    script = Path(__file__).resolve().parents[1] / 'bin/acc-mcp-team'
    result = subprocess.run([sys.executable, '-I', str(script), '--orchestrator', '--orchestrator-v2'],
        input=json.dumps({'id': 1, 'method': 'tools/list'}) + '\n', text=True, capture_output=True,
        env={'ACC_RUN_DIR': str(tmp_path), 'ACC_SESSION_ID': 'synthetic'}, timeout=8)
    assert result.returncode == 0, result.stderr
    tools = json.loads(result.stdout)['result']['tools']
    from acc.server.teams import TEAM_READ_TOOLS
    assert TEAM_READ_TOOLS == guard.READ_ONLY_TEAM_TOOLS == {t['name'] for t in tools if t['annotations']['readOnlyHint']}
    assert guard.PLAN_AUTO_TEAM_TOOLS <= {t['name'] for t in tools}
    assert not guard.PLAN_AUTO_TEAM_TOOLS & {'merge', 'restack', 'gate', 'brief_share', 'brief_propose_documents'}
