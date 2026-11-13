"""Actual main state changes at isolated native decision await barriers."""
import time

from acc.server.db import write_intent
from acc.server.models import Observation, RelayDecision, RelaySession, SecurityRevision, Session, Setting


@write_intent
def change_decision_scope(c, sid, decision_id, fault):
    with c.db() as db:
        if fault == 'positive':
            pass
        elif fault in ('expiry', 'revision'):
            row = db.get(RelayDecision, (sid, decision_id))
            value = dict(row.decision)
            if fault == 'expiry':
                value['expiresAtMs'] = int(time.time() * 1000) - 1
            else:
                value['revision'] += 1
            row.decision = value
        elif fault == 'budget':
            provider = db.get(Session, sid).provider
            window = 'claude.five_hour' if provider == 'claude' else 'codex.primary'
            observation = 'claude.rate_limits' if provider == 'claude' else 'codex.rate_limits'
            value = ({'five_hour': {'used_percentage': 99, 'resets_at': time.time() + 3600}}
                if provider == 'claude' else {'primary': {'used_percent': 99, 'resets_at': time.time() + 3600,
                    'window_minutes': 300}})
            db.merge(Observation(key=observation, value=value, source='fixture', observed_at=time.time()))
            db.merge(Setting(key='budget.alex', value={'rules': {
                window: {'mode': 'cap', 'limit': 1, 'action': 'pause'}}}))
        elif fault == 'policy':
            row = db.get(SecurityRevision, 1)
            if row is None:
                db.add(SecurityRevision(id=1, revision=1))
            else:
                row.revision += 1
        elif fault == 'epoch':
            db.get(RelaySession, sid).authority_epoch += 1
        elif fault == 'stop':
            db.get(Session, sid).desired_state = 'stopped'
        elif fault == 'account':
            db.get(Session, sid).run_as = 'example-other-agent'
        else:
            raise ValueError('Unknown decision-scope fault')
        db.commit()
        if fault == 'budget':
            from acc.server import budgets
            row = db.get(Session, sid)
            assert budgets.paused(db, 'alex', row.provider, project_id=row.project_id,
                machine=row.host or 'local', account=row.run_as), 'Fixture must enforce a real saved budget pause'

    c.native_scope_changed = fault
