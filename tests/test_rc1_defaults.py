"""A fresh installation is closed by default (final RC security audit): Previews, company sign-in, mobile enrollment
and remote access are off; Orchestrator is on by owner decision; first-run setup needs the one-time token; every answer carries the security headers."""
import httpx
import pytest

from acc.server.admin_features import FEATURES, Features
from acc.server.config import Settings

OFF_BY_DEFAULT = ('organizations', 'groups_remote', 'containers', 'orchestrator', 'mobile')


@pytest.mark.parametrize('key', OFF_BY_DEFAULT)
def test_previews_and_company_sign_in_are_off_out_of_the_box(key, monkeypatch):
    for variable in ('ACC_ORGANIZATIONS', 'ACC_GROUPS_REMOTE', 'ACC_CONTAINERS', 'ACC_ORCHESTRATOR', 'ACC_MOBILE',
                     f'ACC_FEATURE_{key.upper()}'):
        monkeypatch.delenv(variable, raising=False)
    from acc.env import environment
    for variable in list(environment):
        if variable.startswith('ACC_FEATURE_') or variable in ('ACC_ORGANIZATIONS', 'ACC_GROUPS_REMOTE',
                                                              'ACC_CONTAINERS', 'ACC_ORCHESTRATOR', 'ACC_MOBILE'):
            monkeypatch.delitem(environment, variable)
    expected = key == 'orchestrator'  # Owner-approved rc.1 default; all remaining previews stay off.
    assert FEATURES[key].default is expected
    assert Features(Settings()).enabled(key) is expected


def test_every_preview_needs_a_signed_in_re_verification_to_turn_on():
    for key, feature in FEATURES.items():
        if feature.preview:
            assert feature.sensitive, key


async def test_a_fresh_instance_answers_closed(api):
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://acc')
    async with anonymous:
        state = (await anonymous.get('/api/auth/state')).json()
        assert state['setup_required'] is True and state['authenticated'] is False
        # Setup without the one-time token from the root-only file is refused, and the throttle counts it.
        wrong = await anonymous.post('/api/auth/setup/begin', json={'setup_token': 'guess'},
                                     headers={'Origin': 'http://acc'})
        assert wrong.status_code in (403, 422)
        for path in ('/api/sessions', '/api/admin-features', '/api/gates', '/api/remote-access'):
            answer = await anonymous.get(path)
            assert answer.status_code in (401, 404), path
        page = await anonymous.get('/')
        csp = page.headers['content-security-policy']
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
        assert "'unsafe-inline'" not in csp.split('style-src')[0] and 'script-src' not in csp  # scripts: self only
        assert page.headers['x-frame-options'] == 'DENY' and page.headers['x-content-type-options'] == 'nosniff'
    from acc.server.remote_access import state
    with api.app.state.ctx.db() as s:
        remote = state(s)
    assert remote['enabled'] is False and remote['desired_enabled'] is False  # no public routing until an owner asks
