"""Names must remain visible in cards, launch choices and audit records."""
import pytest


@pytest.mark.parametrize('name', ['', ' ', '\t\n', '\u2003\u00a0', 'x' * 10000])
@pytest.mark.parametrize('kind', ['project', 'session'])
async def test_invalid_names_are_rejected_without_creating_rows(api, env, project, name, kind):
    if kind == 'project':
        url = '/api/projects'
        body = {'slug': 'blank-probe', 'name': name, 'workspace_path': str(env['workspaces'] / 'demo')}
    else:
        url = '/api/sessions'
        body = {'project_id': project['id'], 'provider': 'claude', 'name': name}
    before = (await api.get(url)).json()
    result = await api.post(url, json=body)
    assert result.status_code == 422, result.text
    assert (await api.get(url)).json() == before


@pytest.mark.parametrize('name', ['Ωmega', '😀', 'שלום', 'مرحبا', 'x' * 40])
async def test_visible_unicode_session_names_remain_supported(api, project, name):
    from helpers import wait_for
    from test_supervisor import state_is

    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': name})
    assert response.status_code == 201
    assert response.json()['name'] == name
    await wait_for(state_is(api, response.json()['id'], 'idle'))
