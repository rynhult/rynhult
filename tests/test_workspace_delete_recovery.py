"""Historical service-account workspaces and already-removed folders."""
import dataclasses
import json
import shutil

from helpers import pause_supervisor, write_db
from test_local_workspace import launch

from acc.broker.server import Broker
from acc.server.api.context import Context
from acc.server.broker_client import BrokerError
from acc.server.models import Session
from acc.server.public_errors import public_error


async def archived_workspace(api, env):
    row = (await launch(api, topic='Old research')).json()
    await api.app.state.supervisor.stop(row['id'], 'test', archive=True)
    await pause_supervisor(api)
    return row, env['workspaces'] / 'local' / row['workdir'].rsplit('/', 1)[1]


async def test_owner_account_history_deletes_through_agents_broker(api, env, monkeypatch):
    row, path = await archived_workspace(api, env)
    with write_db(api.app.state.db) as db:
        db.get(Session, row['id']).run_as = 'human-login'
        db.commit()
    services = Broker(dataclasses.replace(env['broker'], role='services'))
    client = api.app.state.supervisor.broker
    real_call = client.call
    deletions = []

    async def routed(method, account=None, **params):
        if method == 'delete_workspace':
            deletions.append(account)
        if account == 'human-login':
            result = await services.dispatch(json.dumps({'id': 1, 'method': method, 'params': params}).encode())
            if not result['ok']:
                raise BrokerError(result['error'])
            return result['result']
        return await real_call(method, account=account, **params)

    monkeypatch.setattr(client, 'call', routed)
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 200, result.text
    assert deletions == [env['settings'].default_account]
    assert not path.exists()
    assert set(services.methods) == {'ping', 'compose_up', 'compose_ps', 'compose_digest'}


async def test_missing_folder_is_marked_then_detached_with_audit(api, env):
    row, path = await archived_workspace(api, env)
    shutil.rmtree(path)
    entries = (await api.get('/api/workspaces')).json()
    entry = next(entry for entry in entries if entry['folder'] == path.name)
    assert entry['folder_removed'] is True
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 200, result.text
    assert result.json()['already_removed'] is True
    assert path.name not in {entry['folder'] for entry in (await api.get('/api/workspaces')).json()}
    with api.app.state.db() as db:
        assert db.get(Session, row['id']).workdir is None
    audit = (await api.get('/api/audit')).json()
    assert next(item for item in audit if item['action'] == 'workspace.delete')['detail']['already_removed'] is True


async def test_dangling_symlink_is_not_a_missing_folder(api, env, tmp_path):
    row, path = await archived_workspace(api, env)
    shutil.rmtree(path)
    path.symlink_to(tmp_path / 'not-created')
    entry = next(item for item in (await api.get('/api/workspaces')).json() if item['folder'] == path.name)
    assert entry['folder_removed'] is False
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409, result.text
    assert path.is_symlink()
    with api.app.state.db() as db:
        assert db.get(Session, row['id']).workdir is not None


async def test_no_safe_delete_service_is_a_clear_conflict_not_an_internal_rpc_name(api, env, monkeypatch):
    _, path = await archived_workspace(api, env)
    client = api.app.state.supervisor.broker
    real_call = client.call

    async def unavailable(method, **params):
        if method == 'delete_workspace':
            raise BrokerError("RequestError: unknown method 'delete_workspace'")
        return await real_call(method, **params)

    monkeypatch.setattr(client, 'call', unavailable)
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409, result.text
    assert 'owner' in result.text and 'unknown method' not in result.text and 'delete_workspace' not in result.text
    assert path.is_dir()


def test_unknown_broker_method_is_an_internal_error_and_never_exposes_method_name():
    error = BrokerError("RequestError: unknown method 'delete_workspace'")
    assert Context.refused(error).status_code == 500
    assert 'unknown method' not in public_error(error)
    assert 'delete_workspace' not in public_error(error)


async def test_active_legacy_service_session_requires_manual_stop_before_deletion(api, env, monkeypatch):
    row, path = await archived_workspace(api, env)
    with write_db(api.app.state.db) as db:
        item = db.get(Session, row['id'])
        item.run_as, item.desired_state, item.observed_state = 'human-login', 'running', 'idle'
        db.commit()
    client = api.app.state.supervisor.broker
    real_call = client.call
    deleted = []

    async def services(method, account=None, **params):
        if account == 'human-login':
            if method == 'ping':
                return {'role': 'services'}
            raise BrokerError(f"unknown method '{method}'")
        if method == 'delete_workspace':
            deleted.append(params)
        return await real_call(method, account=account, **params)

    monkeypatch.setattr(client, 'call', services)
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409, result.text
    assert 'Stop' in result.text and deleted == [] and path.is_dir()


async def test_workspace_permission_failure_leaves_sessions_attached_and_gives_recovery(api, env, monkeypatch):
    from acc.broker import ops

    row, path = await archived_workspace(api, env)

    def no_access(*args, **kwargs):
        raise PermissionError('simulated account boundary')

    with monkeypatch.context() as patch:
        patch.setattr(ops.shutil, 'rmtree', no_access)
        result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409 and 'original account' in result.text, result.text
    assert path.is_dir()
    with api.app.state.db() as db:
        assert db.get(Session, row['id']).workdir is not None


async def test_symlinked_local_parent_is_not_a_ghost(api, env, tmp_path):
    _, path = await archived_workspace(api, env)
    shutil.rmtree(path.parent)
    path.parent.symlink_to(tmp_path / 'absent-local')
    entry = next(item for item in (await api.get('/api/workspaces')).json() if item['folder'] == path.name)
    assert entry['folder_removed'] is False
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409
    assert path.parent.is_symlink()


async def test_remote_path_is_never_treated_as_a_missing_local_workspace(api, env):
    row, path = await archived_workspace(api, env)
    shutil.rmtree(path)
    with write_db(api.app.state.db) as db:
        db.get(Session, row['id']).placement.host = 'disconnected-node'
        db.commit()
    entry = next(item for item in (await api.get('/api/workspaces')).json() if item['folder'] == path.name)
    assert entry['folder_removed'] is False
    result = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert result.status_code == 409 and 'another machine' in result.text
