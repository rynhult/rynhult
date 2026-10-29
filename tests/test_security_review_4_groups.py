"""Leaving a group revokes consent without broadening removed-member access."""
# ruff: noqa: F811
import pytest
from helpers import write_db
from test_groups import grouped, two  # noqa: F401
from test_users import browser, csrf


@pytest.mark.parametrize('archived', [False, True])
async def test_removed_key_owner_has_no_surviving_consent(grouped, archived):
    from acc.server.models import GroupKeyGrant, VaultKey
    owner, actor = grouped['owner'], grouped['member']
    group_id = grouped['group']['id']
    base = f'/api/groups/{group_id}'
    with write_db(owner.app.state.db) as db:
        db.add(VaultKey(id='owned-key', name='Synthetic owned key', variable='OWN_KEY', ciphertext='sealed', owner='alex'))
        db.add(VaultKey(id='other-key', name='Synthetic private name', variable='OTHER_KEY', ciphertext='other-sealed', owner='casey'))
        db.flush()
        db.add(GroupKeyGrant(key_id='other-key', group_id=group_id, given_by='casey'))
        db.commit()
    consent = await actor.post(base + '/key-grants', json={'revision': 2, 'key_id': 'owned-key'}, headers=csrf(actor))
    assert consent.status_code == 201, consent.text
    grant_id = consent.json()['id']
    assert (await owner.post(base + '/members/1', json={'role': 'maintainer', 'revision': 3})).status_code == 200
    project_id = grouped['group_project']['id']
    assert (await owner.post(base + f'/projects/{project_id}/owners', json={'owners': [1], 'revision': 4})).status_code == 200
    assert (await owner.post(base + '/members/2', json={'role': 'remove', 'revision': 5})).status_code == 200
    assert (await actor.get(base)).status_code == 404
    assert (await actor.get(base + '/people')).status_code == 404
    revision = 6
    if archived:
        assert (await owner.post(base + '/archive', json={'revision': revision})).status_code == 200
        revision += 1
    anonymous = browser(owner)
    try:
        assert (await anonymous.get('/api/groups/key-grants/owned')).status_code == 401
    finally:
        await anonymous.aclose()
    # The other person's consent survives removal, but not group archival.
    with owner.app.state.db() as db:
        from sqlalchemy import select
        assert list(db.scalars(select(GroupKeyGrant.key_id))) == ([] if archived else ['other-key'])
    assert (await owner.get('/api/groups/key-grants/owned')).json() == []
    stale = await actor.post(base + f'/key-grants/{grant_id}/revoke', json={'revision': 3}, headers=csrf(actor))
    assert stale.status_code == 404
    owned = await actor.get('/api/groups/key-grants/owned')
    assert owned.status_code == 200, owned.text
    assert owned.json() == []
    assert 'other-key' not in owned.text and 'sealed' not in owned.text
    revoked = await actor.post(base + f'/key-grants/{grant_id}/revoke', json={'revision': revision}, headers=csrf(actor))
    assert revoked.status_code == 404, revoked.text
    with write_db(owner.app.state.db) as db:
        assert db.get(GroupKeyGrant, grant_id) is None
    assert (await actor.get('/api/groups/key-grants/owned')).json() == []
    assert (await actor.get(base)).status_code == 404
