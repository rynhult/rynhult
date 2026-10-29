"""Backup consent and refusal paths that coverage showed untested."""
from helpers import write_db
from sqlalchemy import select
from test_public_git_backup_policy import FRESH, LEAF, prepare

from acc.backup_server import Refused
from acc.server.models import AuditLog, Node


async def test_turning_a_projects_backup_off_ends_its_public_pushes_at_once(api, project):
    from acc.server.edge import Connection

    prepare(api, project)
    with write_db(api.app.state.db) as db:
        db.get(Node, FRESH).public_git_backup = True
        db.commit()
    edge = api.app.state.ctx.edge
    git = Connection(LEAF, f"/api/git/{project['id']}.git/git-receive-pack")
    control = Connection(LEAF, '/api/nodes/connect')
    edge.open.update([git, control])
    try:
        on = await api.post(f"/api/projects/{project['id']}/backup", json={'enabled': True})
        assert on.status_code == 200, on.text
        assert not git.closed.is_set()  # still consented: nothing is cut
        off = await api.post(f"/api/projects/{project['id']}/backup", json={'enabled': False})
        assert off.status_code == 200, off.text
        # No waiting for the periodic sweep: the open push for this project ends with the change.
        assert git.closed.is_set()
        assert not control.closed.is_set()
    finally:
        edge.open.difference_update([git, control])


def checks(api):
    with api.app.state.db() as s:
        return list(s.scalars(select(AuditLog.detail).where(AuditLog.action == 'backup.target_check')))


async def test_refused_target_check_is_a_409_and_is_audited_as_failed(api, monkeypatch):
    jobs = api.app.state.ctx.backup_jobs
    def refused(*a, **kw):
        raise Refused('The receiver refused this writer key')
    monkeypatch.setattr(jobs.service, 'check_target', refused)
    response = await api.post(f"/api/host/backups/{'f' * 32}/check")
    assert response.status_code == 409 and response.json()['detail'] == 'The receiver refused this writer key'
    assert checks(api) == [{'ok': False}]


async def test_unexpected_target_check_error_never_shows_its_text(api, monkeypatch):
    jobs = api.app.state.ctx.backup_jobs
    def broken(*a, **kw):
        raise RuntimeError('s3 secret_access_key=EXAMPLEKEYDONOTSHOW rejected')
    monkeypatch.setattr(jobs.service, 'check_target', broken)
    response = await api.post(f"/api/host/backups/{'f' * 32}/check")
    assert response.status_code == 409
    assert 'EXAMPLEKEYDONOTSHOW' not in response.text and 'Credentials are never shown' in response.text
    assert checks(api) == [{'ok': False}]
