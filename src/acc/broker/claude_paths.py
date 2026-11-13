"""Broker-owned session placement must precede every Claude factory side effect."""
from acc.session_relay import protocol as p
from acc.session_relay.claude_paths import validate

from . import ops


def check(config, spec):
    sid=spec.spec.get('session_id')
    if not isinstance(sid,str) or not ops.ACC_ID.fullmatch(sid):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    root=config.run_dir/sid
    if spec.spec.get('run_dir')!=str(root):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return validate(spec.spec,root)
