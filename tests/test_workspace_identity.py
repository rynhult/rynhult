"""Account broker identity canonicalizes aliases without exposing folder contents."""
import json

from acc.broker import ops
from acc.broker.server import Broker


def test_identity_alias_outside_workspace_root_is_still_canonical(env):
    local = env['workspaces'] / 'local'
    local.mkdir()
    target = local / 'research'
    target.mkdir()
    alias = env['home'] / 'alias'
    alias.symlink_to(local, target_is_directory=True)
    assert ops.workspace_identity(env['broker'], str(alias / target.name)) == {'path': str(target)}
    # Ordinary registration does not get an existence/directory probe response.
    missing = str(env['workspaces'] / 'missing')
    assert ops.workspace_identity(env['broker'], missing) == {'path': missing}


async def test_identity_is_not_available_from_services_broker(env):
    from dataclasses import replace

    broker = Broker(replace(env['broker'], role='services'))
    result = await broker.dispatch(json.dumps({
        'id': 1, 'method': 'workspace_identity', 'params': {'path': str(env['workspaces'])}}).encode())
    assert result['ok'] is False
