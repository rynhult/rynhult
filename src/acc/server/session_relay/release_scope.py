"""Coordinator's opt-in R1 scope; qualification switches cannot widen it."""
from acc.session_relay import protocol as p


def refuse():
    raise p.RelayError(p.ErrorCode.UNSUPPORTED, details={'reason': 'not_supported_in_release'})


def supported_actor(actor):
    # Scoped API/team service grants retain their existing authorization checks.
    if actor.kind is p.ActorKind.SERVICE and actor.channel is p.Channel.SCHEDULE:
        refuse()


def local(session, actor):
    supported_actor(actor)
    inputs = session.launch_spec.inputs or {}
    # Placement and Claude auth are governed by their existing enrollment policies.
    # Nodes remain supported; subscription still requires the person's risk opt-in.
    if (session.automation
            or inputs.get('profile') not in (None, '', 'default')):
        refuse()
