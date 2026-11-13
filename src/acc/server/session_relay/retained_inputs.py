"""Actor-private presentation of immutable retained Claude input; no authority."""
from acc.session_relay import protocol as p
from acc.session_relay.claude_retained_client import RetainedClaudeClient


def snapshot(ctx,row,actor):
    # Caller has freshly authenticated and checked session read access in its
    # current transaction. Never create a runtime, client or provider on reads.
    from .exclusive_runtime import ExclusiveLaunch
    runtime=getattr(ctx,'relay_runtime',None)
    run=runtime.runs.get(row.session_id) if runtime else None
    if (not isinstance(run,ExclusiveLaunch) or actor.kind is not p.ActorKind.PERSON
            or run.binding_id!=row.binding_id or str(run.run_id)!=str(row.current_run_id)):
        return None
    values=[]
    for key,client in tuple(run.terminal_grants.retained_clients.items())[:16]:
        if (not isinstance(client,RetainedClaudeClient) or client.actor!=actor or not client.read_only
                or key!=(client.run_id,client.attachment_id) or client.session_id!=row.session_id
                or client.native_id!=run.binding.native_conversation):
            continue
        values.append({'sourceRunId':client.run_id,'view':'terminal','text':client.draft,'readOnly':True})
    return values
