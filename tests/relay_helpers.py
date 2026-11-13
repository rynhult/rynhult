"""Synthetic relay fixture: no provider, broker, server or real credential."""
from types import SimpleNamespace

from fastapi import HTTPException

from acc.server import child_caps  # noqa: F401 — register immutable lineage before seeding
from acc.server.api.context import Context, Principal
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker, migrate, write_intent
from acc.server.models import LaunchSpec, Session, SessionRun, User, utcnow
from acc.server.session_relay import store
from acc.session_relay import protocol as p

ACTOR = p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.CHAT)
DIGEST = p.sha256_digest(b'fixture')


def setup(tmp_path, url=None):
    url = url or f'sqlite:///{tmp_path}/relay.db'
    migrate(url)
    engine = make_engine(url)
    dbs = make_sessionmaker(engine)
    @write_intent
    def seed():
        with dbs() as db:
            db.add_all([User(id=1, username='alex', role='owner', password_hash='unused', totp_secret='unused'),
                        User(id=2, username='sam', role='member', password_hash='unused', totp_secret='unused')])
            db.add(LaunchSpec(id=1, provider='codex', inputs={}, files={}, digest='fixture'))
            db.flush()
            db.add(Session(id='relay', provider='codex', name='Fixture', tmux_name='fixture-relay',
                           launch_spec_id=1, run_as='fixture', owner='alex'))
            db.flush()
            db.add(SessionRun(id=1, session_id='relay', reason='launch', verified_at=utcnow()))
            db.flush()
            store.enroll(db, 'relay', 1, 'binding', identity={'fixture': True}, frozen_policy={}, policy_revision=0)
            db.commit()
    seed()
    ctx = SimpleNamespace(db=dbs, settings=Settings(data_dir=tmp_path / 'data', feature_overrides={'session_relay': True}))
    who = Principal('alex', 'owner', 'fixture', 1)
    state = {'who': who, 'actor': ACTOR, 'revoked': False}
    def resolve():
        if state['revoked']:
            raise HTTPException(401, 'Fixture revoked')
        return state['who'], state['actor']
    ctx.session_for = lambda db, who, identifier: Context.session_for(ctx, db, who, identifier)
    ctx.owns = Context.owns
    return SimpleNamespace(ctx=ctx, resolve=resolve, state=state, db=dbs, engine=engine, url=url)



def replace_unlaunched_session(db, session, **changes):
    """Create fixture lineage at insertion, never rewrite a persisted parent."""
    from sqlalchemy import select

    from acc.server.models import RelaySession
    assert db.get(RelaySession, session.id) is None
    assert db.scalar(select(SessionRun.id).where(SessionRun.session_id == session.id)) is None
    values = {column.key: getattr(session, column.key) for column in Session.__table__.columns
              if column.key not in ('parent_chain', 'root_owner')}
    values.update(changes)
    if parent_id := values.get('parent_session_id'):
        import json

        from acc.server.compiler import launch_digest
        parent = db.get(Session, parent_id)
        files = {**session.launch_spec.files, child_caps.FILE: json.dumps(child_caps.proof(db, parent))}
        spec = LaunchSpec(provider=session.launch_spec.provider, inputs=dict(session.launch_spec.inputs),
                          files=files, digest=launch_digest(session.launch_spec.inputs, files))
        db.add(spec)
        db.flush()
        values['launch_spec_id'] = spec.id
    db.delete(session)
    db.flush()
    result = Session(**values)
    db.add(result)
    db.flush()
    return result
