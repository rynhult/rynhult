"""Owner-only Claude relay preview grant (docs/decisions/0074-relay-preview.md)."""
import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from relay_helpers import setup

from acc.server import claude_subscription
from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, SessionRun, utcnow
from acc.server.session_relay import release_qualification as rq
from acc.server.session_relay import writers
from acc.session_relay import protocol as p

OWNER = p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.CHAT)
MEMBER = p.Actor(p.ActorKind.PERSON, 'sam', p.Channel.CHAT)
PREVIEW = {'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified',
           'terminal.input': 'qualified', 'text': 'qualified'}


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={
        'session_relay': True, 'claude_subscription_chat': True})
    @write_intent
    def claude():
        with case.db() as db:
            session = db.get(Session, 'relay')
            session.provider, session.claude_auth = 'claude', 'api_key'
            run = db.get(RelayRun, 1)
            run.identity = {**run.identity, 'cliDigest': rq.DECLARATION.cli_digest}
            run.frozen_policy = {'authMode': 'api_key', 'personId': 1}
            db.commit()
    claude()
    return case


def consent(case, enabled=True):
    @write_intent
    def change():
        with case.db() as db:
            claude_subscription.update(db, Principal('alex', 'owner', 'fixture', 1), enabled, claude_subscription.VERSION)
            db.commit()
    change()


def granted(case, actor=OWNER, change=None):
    """Apply one change inside the transaction, grant, and never commit."""
    with case.db() as db:
        row, run = db.get(RelaySession, 'relay'), db.get(RelayRun, 1)
        session, native = db.get(Session, 'relay'), db.get(SessionRun, 1)
        if change:
            change(SimpleNamespace(row=row, run=run, session=session, native=native))
        value = dict(rq.grant(case.ctx, db, row, run, actor))
        assert row.capabilities == value
        db.rollback()
        return value


def test_declaration_is_frozen_claude_only_preview_pin():
    from acc.broker.claude_owner import CLI_DIGEST
    value = rq.DECLARATION
    assert value.provider == 'claude' and value.cli_digest == CLI_DIGEST
    assert value.adapter_version == 'claude-agent-sdk/0.1.50' and value.qualification_id == rq.QUALIFICATION_ID
    qualified = {name for name in p.CAPABILITIES if value.support(name) is p.Support.QUALIFIED}
    assert qualified == {'text', 'terminal.input', 'switch.to_chat', 'switch.to_terminal'}
    assert value.support('decision.tool_approval') is not p.Support.QUALIFIED
    assert value.support('interrupt') is not p.Support.QUALIFIED
    with pytest.raises(Exception):
        value.qualification_id = 'other'
    with pytest.raises(TypeError):
        value.operations['interrupt'] = value.operations['text']


def test_grant_writes_preview_capabilities_when_every_condition_holds(case):
    assert granted(case) == PREVIEW


def test_subscription_grant_needs_the_persons_current_consent(case):
    def subscription(c):
        c.session.claude_auth = 'subscription'
        c.run.frozen_policy = {'authMode': 'subscription', 'personId': 1}
    assert granted(case, change=subscription) == {}
    consent(case)
    assert granted(case, change=subscription) == PREVIEW
    consent(case, enabled=False)  # revoked consent
    assert granted(case, change=subscription) == {}


def test_subscription_grant_refuses_another_persons_frozen_consent(case):
    consent(case)
    def other(c):
        c.run.frozen_policy = {'authMode': 'subscription', 'personId': 2}
    assert granted(case, change=other) == {}


def test_subscription_grant_refuses_when_owner_disables_subscription_chat(case):
    consent(case)
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={
        'session_relay': True, 'claude_subscription_chat': False})
    def subscription(c):
        c.run.frozen_policy = {'authMode': 'subscription', 'personId': 1}
    assert granted(case, change=subscription) == {}


def _set(name, value):
    def change(c):
        target, attribute = name.split('.')
        setattr(getattr(c, target), attribute, value)
    return change


@pytest.mark.parametrize('change', [
    pytest.param(_set('session.provider', 'codex'), id='codex'),
    pytest.param(_set('row.sealed', True), id='sealed'),
    pytest.param(_set('row.current_run_id', 2), id='not-current-run'),
    pytest.param(_set('row.binding_id', 'other-binding'), id='binding'),
    pytest.param(_set('native.ended_at', utcnow()), id='ended-run'),
    pytest.param(lambda c: setattr(c.run, 'identity', {**c.run.identity, 'cliDigest': p.sha256_digest(b'other')}),
                 id='digest-mismatch'),
    pytest.param(lambda c: setattr(c.run, 'identity', {k: v for k, v in c.run.identity.items() if k != 'cliDigest'}),
                 id='unprobed'),
    pytest.param(_set('session.automation', {'goal': 'automated'}), id='release-scope'),
    pytest.param(_set('session.owner', 'sam'), id='another-persons-session'),
])
def test_grant_writes_nothing_when_one_condition_fails(case, change):
    assert granted(case, change=change) == {}


def test_grant_refuses_member_and_service_actors(case):
    assert granted(case, actor=MEMBER, change=_set('session.owner', 'sam')) == {}
    service = p.Actor(p.ActorKind.SERVICE, 'alex', p.Channel.API)
    assert granted(case, actor=service) == {}
    assert granted(case, actor=None) == {}


def test_grant_refuses_when_owner_role_is_removed(case):
    from acc.server.models import User
    @write_intent
    def demote():
        with case.db() as db:
            db.get(User, 1).role = 'member'
            db.commit()
    demote()
    assert granted(case) == {}


def test_grant_refuses_with_admin_feature_off(case):
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={'session_relay': False})
    assert granted(case) == {}


@pytest.mark.parametrize('flag', ['acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED',
    'acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED',
    'acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED'])
def test_grant_refuses_when_any_preview_flag_is_off(case, monkeypatch, flag):
    monkeypatch.setattr(flag, False)
    assert granted(case) == {}


def test_grant_overwrites_existing_capabilities_on_refusal(case):
    def fabricated(c):
        c.row.capabilities = {'decision.tool_approval': 'qualified', 'interrupt': 'qualified'}
        c.row.sealed = True
    assert granted(case, change=fabricated) == {}


def test_only_preview_flags_changed_every_other_qualification_stays_off():
    from acc.broker import claude_allow_evidence, claude_decision_journal, claude_denial_evidence
    from acc.server.session_relay import (
        claude_public,
        delivery,
        exclusive_public,
        exclusive_recovery,
        interrupts,
        reconciliation,
        stock_approval_frames,
    )
    from acc.session_relay import claude_answer_evidence, native_approval_aliases, stock_approval_input
    assert exclusive_public.CLAUDE_LAUNCH_QUALIFIED is True
    assert claude_public.PUBLIC_CLAUDE_QUALIFIED is True
    assert writers.PUBLIC_WRITER_QUALIFIED is True
    assert writers.CODEX_PUBLIC_WRITER_QUALIFIED is True
    assert writers.qualified('claude') is True
    assert writers.qualified('codex') is True and writers.qualified('opencode') is False
    from acc.server.session_relay import native_decisions
    assert native_decisions.CLAUDE_ENROLLED_DECISIONS_QUALIFIED is False
    assert native_decisions.CODEX_ENROLLED_DECISIONS_QUALIFIED is False
    for value in (claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED, interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED,
                  exclusive_public.PUBLIC_L1_QUALIFIED, exclusive_recovery.RECOVERY_QUALIFIED,
                  delivery.DELIVERY_QUALIFIED, delivery.CLAUDE_SERVICE_QUALIFIED, reconciliation.QUALIFIED,
                  stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED, stock_approval_input.STOCK_APPROVAL_INPUT_QUALIFIED,
                  native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED, claude_answer_evidence.CLAUDE_ACK_QUALIFIED,
                  claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED, claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED,
                  claude_decision_journal.CLAUDE_DURABLE_EVIDENCE_QUALIFIED):
        assert value is False


def test_only_launch_and_switch_commit_call_the_grant_recovery_never_regrants():
    root = Path(rq.__file__).parent
    callers = set()
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'release_qualification' and node.level == 1:
                if any(alias.name == 'grant' for alias in node.names):
                    callers.add(path.name)
    assert callers == {'runtime.py', 'ownership.py'}
    # Fence loss, maintenance and recovery keep their unconditional resets.
    for name, text in (('runtime.py', 'row.capabilities = {}'), ('maintenance.py', 'row.capabilities = {}'),
                       ('exclusive_recovery.py', 'row.capabilities={}')):
        assert text in (root / name).read_text(), name
    assert 'release_qualification' not in (root / 'exclusive_recovery.py').read_text()
    assert 'release_qualification' not in (root / 'maintenance.py').read_text()


def test_codex_native_decisions_stay_closed_with_fabricated_capability(case):
    from acc.server.session_relay import native_decisions
    with case.db() as db:
        row = db.get(RelaySession, 'relay')
        row.capabilities = {'decision.tool_approval': 'qualified', 'text': 'qualified', 'terminal.input': 'qualified'}
        db.get(Session, 'relay').provider = 'codex'
        runtime = SimpleNamespace(runs={'relay': SimpleNamespace(provider='codex')})
        ctx = SimpleNamespace(settings=case.ctx.settings, relay_runtime=runtime)
        assert native_decisions.public_ready(ctx, row) is False
        assert writers.public_ready(ctx, row) is True  # Codex writer preview is now separately enabled
        db.get(Session, 'relay').provider = 'claude'
        runtime.runs['relay'].provider = 'claude'
        assert writers.public_ready(ctx, row) is True
        db.expunge(row)
        assert writers.public_ready(ctx, row) is False  # detached rows fail closed
        db.rollback()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_member_relay_launch_is_refused_with_visible_owner_only_message(api, project, env, provider):
    from helpers import write_db
    from sqlalchemy import select
    from test_auth import cookie_client, do_setup

    from acc.server.models import User
    await do_setup(api, env)
    client = cookie_client(api)
    headers = {'Origin': 'http://acc', 'X-ACC-CSRF': client.cookies.get('acc_csrf')}
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'casey')).role = 'member'
        db.commit()
    api.app.state.ctx.settings = replace(api.app.state.ctx.settings, feature_overrides={
        **api.app.state.ctx.settings.feature_overrides, 'session_relay': True})
    body = {'project_id': project['id'], 'provider': provider, 'name': 'Member relay', 'session_relay': True,
            'worktree': False, **({'claude_auth': 'api_key'} if provider == 'claude' else {})}
    reply = await client.post('/api/sessions', json=body, headers=headers)
    assert reply.status_code == 403, reply.text
    assert reply.json()['detail'] == rq.OWNER_ONLY
    with api.app.state.db() as db:
        assert not db.query(RelaySession).count()


def test_reserve_refuses_a_member_before_any_run_is_reserved(tmp_path):
    from acc.server.session_relay.runtime import RuntimeManager
    from acc.server.supervisor_common import LaunchRefused
    case = setup(tmp_path)
    case.ctx.broker = SimpleNamespace(runtime_managers=set(), authority=None)
    case.state['who'] = Principal('sam', 'member', 'fixture', 2)
    case.state['actor'] = MEMBER
    @write_intent
    def unlaunched():
        with case.db() as db:
            db.delete(db.get(RelayRun, 1))
            db.delete(db.get(RelaySession, 'relay'))
            db.delete(db.get(SessionRun, 1))
            db.get(Session, 'relay').owner = 'sam'
            db.commit()
    unlaunched()
    launch = RuntimeManager(case.ctx).new_exclusive_launch(case.resolve)
    with case.db() as db, pytest.raises(LaunchRefused, match=rq.OWNER_ONLY):
        launch.reserve(db, db.get(Session, 'relay'))
    assert launch.run_id is None and not launch.manager.launches
    with case.db() as db:
        assert db.get(RelaySession, 'relay') is None and not db.query(SessionRun).count()


def test_every_claude_agent_options_construction_pins_an_absolute_cli_path():
    """The SDK's bundled or PATH-searched CLI is never used: one guarded constructor."""
    from acc.session_relay.adapters import claude_agent
    src = Path(claude_agent.__file__).resolve().parents[2]
    constructions = []
    for path in src.rglob('*.py'):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'ClaudeAgentOptions':
                    constructions.append((path.name, function.name))
    assert constructions == [('claude_agent.py', 'pinned_options')]
    for options in ({}, {'cli_path': None}, {'cli_path': 'claude'}, {'cli_path': 'bin/claude'}, {'cli_path': b'/usr/bin/claude'}):
        with pytest.raises(p.RelayError) as error:
            claude_agent.pinned_options(options)
        assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE


def test_sdk_client_and_preview_route_through_the_pinned_constructor(monkeypatch):
    pytest.importorskip('claude_agent_sdk')
    from acc.server.compiler import LaunchInputs
    from acc.server.session_relay.claude_public import preview_args
    from acc.session_relay.adapters import claude_agent
    seen = []
    original = claude_agent.pinned_options
    def recording(options):
        seen.append(options['cli_path'])
        return original(options)
    monkeypatch.setattr(claude_agent, 'pinned_options', recording)
    args = preview_args(LaunchInputs.from_dict({'provider': 'claude'}), {})
    assert '--permission-prompt-tool' in args and seen == ['/nonexecuting/claude-preview']
    with pytest.raises(p.RelayError):
        claude_agent.sdk_client({'cli_path': 'claude'})
