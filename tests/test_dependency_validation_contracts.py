"""Framework-upgrade contracts at the actual API boundary, including secret-free collector refusals."""
import sqlite3

import pytest
from test_dns_infrastructure import owner_client
from test_telemetry_collector_security import client, signed

from acc import telemetry_collector as collector
from acc.server.models import Session


@pytest.mark.parametrize('body', [b'', b'null', b'[]', b'"not an object"'])
async def test_missing_or_non_object_session_body_never_launches(api, project, body):
    response = await api.post('/api/sessions', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 422, response.text
    errors = response.json()['detail']
    assert isinstance(errors, list) and errors
    assert all(error['loc'][0] == 'body' and isinstance(error['msg'], str) for error in errors)
    with api.app.state.db() as db:
        assert db.query(Session).count() == 0


@pytest.mark.parametrize('body', [{}, {'confirmed': None}, {'confirmed': False}, {'confirmed': 'true'}])
async def test_confirmation_defaults_do_not_authorize_export(api, body, monkeypatch):
    def forbidden():
        pytest.fail('an invalid confirmation reached the export')
    monkeypatch.setattr(api.app.state.ctx.backup_jobs.service, 'kit', forbidden)
    response = await api.post('/api/host/backups/kit', json=body)
    assert response.status_code == 422, response.text


async def test_dns_framework_validation_never_echoes_secret_input(api, env):
    headers = await owner_client(api, env)
    canary = 'EXAMPLE-VALIDATION-SECRET'
    response = await api.post('/api/dns/credentials', headers=headers, json={
        'name': [], 'provider': 'cloudflare', 'purpose': 'certificates',
        'fields': {'api_token': canary}, 'password': canary})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': "Some DNS settings aren't valid. Check the form and try again."}
    assert canary not in response.text


@pytest.mark.parametrize('body', [b'null', b'[]', b'{"password":"EXAMPLE-VALIDATION-SECRET"}', b'{'])
async def test_signed_invalid_collector_json_is_generic_and_never_stored(tmp_path, body):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = tmp_path / 'collector.db'
    app = collector.create_collector(path)
    private = Ed25519PrivateKey.generate()
    async with client(app) as browser:
        response = await browser.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))
    assert response.status_code == 422
    assert response.json() == {'detail': 'Invalid payload'}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM payload').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM signing_key').fetchone()[0] == 0
