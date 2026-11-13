"""Read an existing owned native screen with fresh opaque observer authority."""
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun


async def read(runtime, params):
    if params.keys() != {'grant'}:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    runtime.guard_check()
    link = runtime.broker.link
    if link is None:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    before = await runtime.peer.call('authority.terminal_read', params)
    if (before['sessionId'] != runtime.spec.spec['session_id'] or before['runId'] != runtime.spec.run_id
            or before['bindingId'] != runtime.binding.binding_id
            or before['epoch'] != link[0].authority_epoch):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    authorize = None
    if runtime.binding.provider == 'claude':
        observer = runtime.owners.observer
        async def authorize():
            runtime.guard_check()
            runtime.owners.guard_check()
            current = await runtime.peer.call('authority.terminal_read', params)
            runtime.guard_check()
            runtime.owners.guard_check()
            return (current == before and runtime.broker.link == link and not runtime.peer.closed
                    and runtime.owners.observer is observer and runtime.controller.switch.view == 'terminal')
    result = await capture_owned(runtime, TerminalRun(before['sessionId'], before['runId']), authorize=authorize)
    after = await runtime.peer.call('authority.terminal_read', params)
    runtime.guard_check()
    if runtime.broker.link != link or runtime.peer.closed or before != after:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return {'runId': before['runId'], **result}


async def capture_owned(runtime, run, *, authorize=None):
    """Capture only an already-owned view; callers provide read/finalize authority."""
    observer = runtime.owned_view
    if runtime.binding.provider == 'claude':
        observer = runtime.owners.observer if runtime.controller.switch.view == 'terminal' else None
    if observer is None:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    if runtime.binding.provider == 'claude' and authorize is not None:
        result = await observer.capture(run, authorize=authorize)
    else:
        result = await observer.capture(run)
    if runtime.binding.provider == 'codex' and getattr(runtime, 'owned_generations', None):
        sections = [result['text']]
        for view, host in runtime.owned_generations:
            if view is observer:
                continue
            if not host.retired:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            old = await view.capture(run)
            sections.append('\nPermanently read-only previous client — unsent draft retained; never replayed.\n' + old['text'])
            result['truncated'] = result['truncated'] or old['truncated']
        text = '\n'.join(sections)
        if len(text.encode()) > 128 << 10:
            raise p.RelayError(p.ErrorCode.LIMIT)  # never silently drop a retained draft
        result = {**result, 'text': text}
    return result
