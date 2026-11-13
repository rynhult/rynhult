"""One authorized read of an existing exact journal entry; no native IO."""
from acc.session_relay import protocol as p


async def read(runtime, params):
    if params.keys() != {'grant'}:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    runtime.guard_check()
    link = runtime.broker.link
    if link is None:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    before = await runtime.peer.call('authority.receipt_read', params)
    identity = before['identity']
    if (before['bindingId'] != runtime.binding.binding_id or identity['sessionId'] != runtime.spec.spec['session_id']
            or identity['runId'] != runtime.spec.run_id or identity['method'] != 'turn.interrupt'
            or identity['fence'] != p.to_wire(link[0])):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    value = runtime.journal.observe(identity)
    result = {'outcome': 'unknown', 'evidence': None}
    if value is not None and value[0] in ('accepted', 'rejected') and value[1] is not None:
        stored = value[1]
        if stored['outcome'] != value[0]:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        proof = stored['evidence']
        result = {'outcome': stored['outcome'], 'evidence': {'kind': proof['kind'],
            'operationId': proof['operation_id'], 'runId': proof['run_id'], 'nativeRef': proof.get('native_ref')}}
    after = await runtime.peer.call('authority.receipt_read', params)
    runtime.guard_check()
    if before != after or runtime.broker.link != link or runtime.peer.closed:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return result
