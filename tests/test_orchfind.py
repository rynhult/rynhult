"""Owner orchestrator findings; isolated Git, broker and API fixtures only."""
import hashlib
from dataclasses import replace

import pytest
from test_team_git_hardening import config, git, lead_and_helper

from acc.broker import ops


@pytest.mark.parametrize("tracked", [False, True])
def test_attested_brief_export_does_not_block_unrelated_helper_merge(tmp_path, tracked):
    root, helper = lead_and_helper(tmp_path)
    (helper / 'feature.txt').write_text('new feature\n')
    git(helper, 'add', 'feature.txt')
    git(helper, 'commit', '-qm', 'feature')
    (root / 'docs').mkdir()
    export = root / 'docs/PROJECT-BRIEF.md'
    if tracked:
        export.write_text('old snapshot\n')
        git(root, 'add', 'docs/PROJECT-BRIEF.md')
        git(root, 'commit', '-qm', 'old snapshot')
    git(root, 'config', 'user.name', 'Taylor')
    git(root, 'config', 'user.email', 'taylor@example.com')
    export.write_text('Person-approved snapshot\n')
    result = ops.team_merge(replace(config(tmp_path), run_dir=tmp_path / 'run'), str(root), 'acc/helper', 'acc/lead',
                            generated_exports={'docs/PROJECT-BRIEF.md': hashlib.sha256(export.read_bytes()).hexdigest()})
    assert result['merged'] and export.read_text() == 'Person-approved snapshot\n'
    assert (root / 'feature.txt').exists()


def test_no_remote_is_a_fact_without_exposing_urls(tmp_path):
    root, _ = lead_and_helper(tmp_path)
    assert ops.git_refs(config(tmp_path), str(root))['has_remote'] is False
    git(root, 'remote', 'add', 'origin', 'https://example.com/demo.git')
    result = ops.git_refs(config(tmp_path), str(root))
    assert result['has_remote'] is True and 'example.com' not in str(result)


async def test_need_cannot_be_completed_empty_and_api_author_is_human(api, project, env, monkeypatch):
    monkeypatch.setenv("ACC_ORCHESTRATOR", "1")
    monkeypatch.setenv("ACC_ORCHESTRATOR_V2", "1")
    from test_team import lead

    from acc.server.orchestrator import worklist
    x = await lead(api, project, env)
    base = f"/api/teams/{x['id']}/worklist"
    created = await api.post(base + '/items', json={'kind': 'need', 'title': 'Type the poster text'})
    assert created.status_code == 201, created.text
    card = created.json()
    response = await api.patch(base + f"/items/{card['id']}", json={'revision': card['revision'], 'state': 'done'})
    assert response.status_code == 422
    response = await api.patch(base + f"/items/{card['id']}", json={'revision': card['revision'], 'answer': 'Skip this reference.'})
    assert response.status_code == 200 and response.json()['answered_by'] == 'Owner via API'
    with api.app.state.ctx.db() as db:
        assert db.get(worklist.WorkItem, card['id']).answered_by == '@api'  # audit identity stays authentic


@pytest.mark.parametrize('change', ['independent', 'staged', 'other', 'symlink'])
def test_export_exception_refuses_unattested_or_staged_work(tmp_path, change):
    root, helper = lead_and_helper(tmp_path)
    export = root / 'docs/PROJECT-BRIEF.md'
    export.parent.mkdir()
    export.write_text('approved\n')
    digest = hashlib.sha256(export.read_bytes()).hexdigest()
    if change == 'independent':
        export.write_text('person editing\n')
    elif change == 'staged':
        git(root, 'add', 'docs/PROJECT-BRIEF.md')
    elif change == 'other':
        (root / 'unrelated.txt').write_text('do not lose me')
    else:
        export.unlink()
        export.symlink_to(helper / 'both.txt')
    before = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(ops.OpError):
        ops.team_merge(replace(config(tmp_path), run_dir=tmp_path / 'run'), str(root), 'acc/helper', 'acc/lead',
                       generated_exports={'docs/PROJECT-BRIEF.md': digest})
    assert git(root, 'rev-parse', 'HEAD') == before
    if change == 'other':
        assert (root / 'unrelated.txt').read_text() == 'do not lose me'


@pytest.mark.parametrize('tracked', [False, True])
def test_merge_conflicts_preserve_export_and_original_head(tmp_path, tracked):
    root, helper = lead_and_helper(tmp_path)
    export = root / 'docs/PROJECT-BRIEF.md'
    export.parent.mkdir()
    export.write_text('old\n')
    if tracked:
        git(root, 'add', 'docs/PROJECT-BRIEF.md')
        git(root, 'commit', '-qm', 'old export')
    export.write_text('approved snapshot\n')
    (root / 'both.txt').write_text('lead\n')
    git(root, 'add', 'both.txt')
    git(root, 'commit', '-qm', 'lead work')
    (helper / 'both.txt').write_text('helper\n')
    git(helper, 'add', 'both.txt')
    git(helper, 'commit', '-qm', 'helper work')
    git(root, 'config', 'user.name', 'Taylor')
    git(root, 'config', 'user.email', 'taylor@example.com')
    before = git(root, 'rev-parse', 'HEAD')
    result = ops.team_merge(replace(config(tmp_path), run_dir=tmp_path / 'run'), str(root), 'acc/helper', 'acc/lead',
                            generated_exports={'docs/PROJECT-BRIEF.md': hashlib.sha256(export.read_bytes()).hexdigest()})
    assert not result['merged'] and result['conflicts'] == ['both.txt'], result
    assert git(root, 'rev-parse', 'HEAD') == before and export.read_text() == 'approved snapshot\n'


@pytest.mark.parametrize('reserved', [False, True])
async def test_actual_lead_launch_delivers_port_and_no_remote_facts(api, project, env, monkeypatch, reserved):
    from helpers import wait_for
    from test_team import lead

    from acc.server.models import PortReservation
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    if reserved:
        with api.app.state.ctx.db() as db:
            db.add(PortReservation(port=8102, project_id=project['id']))
            db.commit()
    root = await lead(api, project, env)
    prompt = env['run_dir'] / root['id'] / 'append-prompt.md'
    await wait_for(lambda: prompt.exists())
    text = prompt.read_text()
    assert 'Git remote: none.' in text and 'local merge/handoff' in text
    assert '8102' in text if reserved else 'No ports are reserved' in text
    assert 'never' in text and 'reserve' in text


async def test_server_passes_only_its_known_export_digest_to_real_merge(api, project, env):
    from pathlib import Path

    from test_team import lead, request

    from acc.server.orchestrator.project_briefs import ProjectBrief
    x = await lead(api, project, env)
    helper = (await request(api, env, x, 'start', name='Worker', agent='claude', task='bounded change'))['result']
    folder = Path(helper['worktree'])
    (folder / 'result.txt').write_text('done\n')
    git(folder, 'add', 'result.txt')
    git(folder, 'commit', '-qm', 'worker result')
    assert (await request(api, env, x, 'stop', helper=helper['id']))['ok']
    export = Path(x['worktree']) / 'docs/PROJECT-BRIEF.md'
    export.parent.mkdir(exist_ok=True)
    export.write_text('Person-approved export\n')
    with api.app.state.ctx.db() as db:
        head = db.get(ProjectBrief, project['id'])
        if head is None:
            head = ProjectBrief(project_id=project['id'])
            db.add(head)
        head.export_path = 'docs/PROJECT-BRIEF.md'
        head.export_digest = hashlib.sha256(export.read_bytes()).hexdigest()
        db.commit()
    reply = await request(api, env, x, 'merge', helper=helper['id'])
    assert reply['ok'] and reply['result']['merged'], reply
    assert export.read_text() == 'Person-approved export\n'
    assert (Path(x['worktree']) / 'result.txt').read_text() == 'done\n'


@pytest.mark.parametrize('stop_kind', ['session', 'team'])
async def test_stop_during_remote_fact_lookup_must_prevent_launch(api, project, env, monkeypatch, stop_kind):
    from helpers import pause_supervisor
    from test_team import lead

    from acc.server.models import Session

    await pause_supervisor(api)
    made = await lead(api, project, env)
    sup = api.app.state.supervisor
    original = sup.broker.call
    stopped, launches = [], []

    async def call(method, **params):
        if method == 'git_refs':
            if stop_kind == 'team':
                await sup.stop_team(made['id'], '@api')
            else:
                await sup.stop(made['id'], '@api')
            with sup.db() as fresh:
                state = fresh.get(Session, made['id']).desired_state
            assert state == 'stopped'
            stopped.append(state)
            return {'git': True, 'has_remote': False}
        if method == 'launch':
            with sup.db() as fresh:
                launches.append(fresh.get(Session, made['id']).desired_state)
            return {'pid': 123, 'command': ['fixture-only; not executed']}
        return await original(method, **params)

    monkeypatch.setattr(sup.broker, 'call', call)
    with sup.db() as db:
        await sup._start(db, db.get(Session, made['id']), reason='launch')
        db.commit()
    assert stopped == ['stopped']
    assert not launches, f'broker launch dispatched after completed Stop: {launches}'


@pytest.mark.parametrize(('field', 'value'), [('run_as', 'other-agent'), ('host', 'other-machine')])
async def test_remote_fact_lookup_rechecks_the_launch_account_and_machine(api, project, env, monkeypatch, field, value):
    from helpers import pause_supervisor, write_db
    from test_team import lead

    from acc.server.compiler import CompileError
    from acc.server.models import Session

    await pause_supervisor(api)
    made = await lead(api, project, env)
    sup = api.app.state.supervisor
    original = sup.broker.call
    lookups, launches = [], []

    async def call(method, **params):
        if method == 'git_refs':
            lookups.append((params['host'], params['account']))
            with write_db(sup.db) as fresh:
                row = fresh.get(Session, made['id'])
                if field == 'host':
                    row.placement.host = value
                else:
                    setattr(row, field, value)
                fresh.commit()
            return {'git': True, 'has_remote': True}
        if method == 'launch':
            launches.append(params)
            return {'pid': 123, 'command': ['fixture-only; not executed']}
        return await original(method, **params)

    monkeypatch.setattr(sup.broker, 'call', call)
    with sup.db() as db:
        row = db.get(Session, made['id'])
        account = row.run_as
        with pytest.raises(CompileError, match='machine or account changed'):
            await sup._start(db, row, reason='launch')
    assert lookups == [('local', account)]
    assert not launches
