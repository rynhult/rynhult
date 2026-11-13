"""Dormant Claude L1 HTTP switch binding; no automatic qualification promotion.

The R1 method set is unchanged. Only a separately qualified Claude L1 placement
may negotiate this extension, and production qualification remains false.
"""
from __future__ import annotations

from acc.session_relay import protocol as p

from ..admin_features import enabled
from . import authority, history, store
from .runtime import RequestResolver

PUBLIC_CLAUDE_QUALIFIED = False
PUBLIC_CLAUDE_DECISIONS_QUALIFIED = False
METHODS = frozenset({'switch.prepare', 'switch.commit', 'switch.cancel'})


def supported(ctx, row):
    from .exclusive_runtime import ExclusiveLaunch
    runtime = getattr(ctx, 'relay_runtime', None)
    run = runtime.runs.get(row.session_id) if runtime else None
    return (PUBLIC_CLAUDE_QUALIFIED is True and not row.sealed and isinstance(run, ExclusiveLaunch)
            and run.peer is not None and not run.peer.closed)


def ready(ctx, row):
    return (supported(ctx, row) and enabled(ctx.settings, 'session_relay')
            and all(row.capabilities.get('switch.to_' + view) == 'qualified' for view in ('chat', 'terminal')))


def offered(ctx, row):
    runtime = getattr(ctx, 'relay_runtime', None)
    run = runtime.runs.get(row.session_id) if runtime else None
    return (({'switch'} if ready(ctx, row) or supported(ctx, row) and run.prepared_plans else set())
        | ({'decisions'} if decisions_ready(ctx, row) else set()))


def admit(ctx, row, command, negotiated):
    target = command.params['method'] if command.method == 'operation.register' else command.method
    if target not in METHODS:
        return False
    if ('switch' not in negotiated.features or not supported(ctx, row)
            or target != 'switch.cancel' and not ready(ctx, row)):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    return True


class SwitchResolver(RequestResolver):
    def __init__(self, current, method):
        super().__init__(current.ctx, current.request, current.attached, attachment_id=current.attachment_id)
        self.method = method

    def in_transaction(self, db):
        result = super().in_transaction(db)
        # Attachment is transport-authenticated, never taken from the switch body.
        if not p.valid_opaque(self.attachment_id):
            raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        row = store.get(db, self.session_id)
        if not supported(self.ctx, row) or self.method != 'switch.cancel' and not ready(self.ctx, row):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return result

    def owner_scope(self):
        # Same cookie/device verifier. This is execution ownership, never input
        # permission; the old attachment is intentionally invalid on the new run.
        return RequestResolver(self.ctx, self.request)


def bind(current, session_id, method):
    value = SwitchResolver(current, method)
    value.session_id = session_id
    return value


async def dispatch(ctx, current, session_id, command):
    from ..db import write_in_thread
    current = bind(current, session_id, command.params['method'] if command.method == 'operation.register' else command.method)
    run = ctx.relay_runtime.runs.get(session_id)
    if run is None:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    params = command.params
    if command.method == 'operation.register':
        return await write_in_thread(lambda: run.owner_hub.register_switch(current, session_id, params['expectedRunId'],
            params['registrationKey'], params['method'], params['payloadDigest']))
    if command.method == 'switch.prepare':
        result = await run.prepare_owner(current, target=params['target'], revision=params['expectedRevision'],
            operation_id=params['operationId'])
        if result['phase'] == 'prepared':
            run.prepared_plans[result['planId']]['_ownerResolve'] = current.owner_scope()
        return result
    if command.method == 'switch.commit':
        try:
            await run.commit_owner(current, params['planId'], params['planDigest'],
                revision=params['expectedRevision'], operation_id=params['operationId'])
        except (ConnectionError, OSError, TimeoutError):
            pass  # Observe the durable outcome below; never dispatch again.
        return await write_in_thread(lambda: committed(ctx, current, session_id, params))
    return await run.cancel_owner(current, params['planId'], operation_id=params['operationId'])


def committed(ctx, current, session_id, params):
    """Receipt observation after run replacement, never renewal of old input."""
    from ..models import RelayOwnership
    resolve = current.owner_scope()
    with ctx.db() as db:
        row, grant = authority.current(ctx, db, resolve, session_id)
        op = store.operation(db, session_id, params['operationId'], grant.actor)
        payload = {key: value for key, value in params.items() if key != 'operationId'}
        p.check_registered(store.receipt_from(op.receipt), actor=grant.actor, session_id=session_id,
            run_id=params['expectedRunId'], method='switch.commit', payload_digest=p.operation_digest('switch.commit', payload))
        if op.receipt['state'] == 'failed':
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED, operation_id=op.id)
        if op.receipt['state'] == 'uncertain':
            public = store.receipt_from(history.public_actors(op.receipt))
            raise p.RelayError(p.ErrorCode.OUTCOME_UNKNOWN, receipt=public)
        if op.receipt['state'] != 'accepted':
            raise p.RelayError(p.ErrorCode.OFFLINE, operation_id=op.id)
        owner = db.get(RelayOwnership, session_id)
        run = ctx.relay_runtime.runs[session_id]
        return {**run.owner_hub.snapshot(row, owner), 'active': not row.sealed,
            'receipt': op.receipt, 'changed': True}


def preview_args(inputs, files):
    """Pinned SDK command construction only: no connect, credential or CLI probe."""
    from dataclasses import replace
    from importlib.metadata import PackageNotFoundError, version
    from types import SimpleNamespace

    from acc.providers.base import CompileError
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
    try:
        if version('claude-agent-sdk') != '0.1.50':
            raise CompileError('This Claude Agent SDK installation is not qualified for relay enrollment.')
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    except (ImportError, PackageNotFoundError):
        raise CompileError('Claude relay requires the qualified Agent SDK installation.') from None
    spec = {'cli_path': 'claude', 'settings_path': '<run-dir>/settings.json', 'initial_chat': True,
        'permission_mode': inputs.permission_mode, 'model': inputs.model, 'effort': inputs.effort}
    for filename, key in (('append-prompt.md', 'append_prompt_path'), ('mcp.json', 'mcp_config_path'),
                          ('plugin/.claude-plugin/plugin.json', 'plugin_path')):
        if filename in files:
            spec[key] = '<run-dir>/' + ('plugin' if key == 'plugin_path' else filename)
    adapter = ClaudeAgentAdapter(policy=lambda *_: False, publish=None)
    adapter.spec = SimpleNamespace(spec=spec, binding=SimpleNamespace(
        workspace='<workspace>', native_conversation='<native-id>'))
    # SDKClient.connect sets stdio for the can_use_tool callback before creating
    # the transport. Mirror that pure transformation; never call connect here.
    options = replace(ClaudeAgentOptions(**adapter.options()), permission_prompt_tool_name='stdio')
    return SubprocessCLITransport(prompt=None, options=options)._build_command()[1:]


def decisions_ready(ctx, row):
    return (PUBLIC_CLAUDE_DECISIONS_QUALIFIED is True and supported(ctx, row)
        and row.capabilities.get('decision.tool_approval') == 'qualified')


class DecisionResolver(RequestResolver):
    def __init__(self, current, session_id):
        super().__init__(current.ctx, current.request, current.attached, attachment_id=current.attachment_id)
        self.session_id = session_id

    def in_transaction(self, db):
        result = super().in_transaction(db)
        if not p.valid_opaque(self.attachment_id) or not decisions_ready(self.ctx, store.get(db, self.session_id)):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return result


async def answer(run, current, params):
    # The hub resolves the server-held choice effect and exact CAS. The broker
    # rechecks this attached resolver before releasing its one native answer.
    # A hook/socket write still returns unknown; this binding never promotes it.
    return await run.answer(DecisionResolver(current, run.session_id), params)
