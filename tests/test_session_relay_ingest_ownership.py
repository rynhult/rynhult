"""Legacy activity cannot mutate owned relay lifecycle or permission state."""
from types import SimpleNamespace

import pytest
from relay_helpers import setup

from acc.server.db import write_intent
from acc.server.ingest import IngestMixin
from acc.server.models import Event, Session, SessionRun


@pytest.mark.parametrize('event', ['SessionStart', 'SessionEnd', 'Stop', 'PermissionRequest'])
def test_relay_spool_is_retained_without_second_lifecycle_authority(tmp_path, event):
    c = setup(tmp_path)
    @write_intent
    def check():
        with c.db() as db:
            session = db.get(Session, 'relay')
            session.desired_state, session.observed_state = 'running', 'running'
            before = session.native_id
            IngestMixin._apply_hook(SimpleNamespace(), db, session, {'provider': 'claude',
                'hook': {'hook_event_name': event, 'session_id': 'another-old-native-id'}})
            db.commit()
            assert session.desired_state == session.observed_state == 'running'
            assert session.native_id == before and db.get(SessionRun, 1).ended_at is None
            assert db.query(Event).filter(Event.kind == event).count() == 1
    try:
        check()
    finally:
        c.engine.dispose()
