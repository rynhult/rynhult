"""Original-binding main authority is independent of a fresh writer/person."""
import copy
from types import SimpleNamespace

import pytest
from relay_helpers import setup

from acc.server.db import write_intent
from acc.server.models import RelayRun, Session
from acc.server.session_relay.reconciliation import OriginalEvidence
from acc.server.vault import Cipher
from acc.session_relay import protocol as p


@pytest.fixture
def original(tmp_path):
    c=setup(tmp_path)
    installation=p.sha256_digest(Cipher(c.ctx.settings.data_dir/'secrets/session_relay_cursor.key').key(create=True))
    placement=['local','fixture',['local'],None,None,str(tmp_path/'runs/relay')]
    @write_intent
    def configure():
        with c.db() as db:
            session=db.get(Session,'relay');session.provider='claude';session.native_id='native';session.workdir=str(tmp_path)
            session.claude_auth='api_key'
            session.run_dir=placement[-1]
            binding=vars(p.Binding('binding',installation,'local','fixture',session.cwd,'claude','native',p.sha256_digest(b'policy')))
            run=db.get(RelayRun,1);run.identity={**run.identity,'binding':binding,'placement':placement}
            run.frozen_policy={'launchDigest':session.launch_spec.digest,'guardDigest':binding['launch_policy_digest'],
                'authMode':'api_key','ownershipMode':'exclusive_idle'}
            db.commit()
    configure()
    manager=SimpleNamespace(ctx=c.ctx,current_placement=lambda db,session,provider,**_options:copy.deepcopy(placement))
    c.evidence=OriginalEvidence(manager,'relay',1);c.placement=placement
    yield c
    c.engine.dispose()


def test_original_scope_uses_no_person_or_writer_grant(original):
    c=original;c.state['revoked']=True
    value=c.evidence.check()
    assert value['binding']['binding_id']=='binding' and value['runId']=='1'
    assert value['binding']['native_conversation']=='native'
    assert not hasattr(c.evidence,'owner') and not hasattr(c.evidence,'resolve')


@pytest.mark.parametrize('fault',['account','workspace','native','binding','installation','policy','guard','node','placement','changed_after_enroll'])
def test_foreign_original_scope_refuses_before_publication(original,fault):
    c=original
    if fault=='changed_after_enroll':c.evidence.check()
    @write_intent
    def change():
        with c.db() as db:
            session=db.get(Session,'relay');run=db.get(RelayRun,1)
            if fault=='account':session.run_as='foreign'
            elif fault=='workspace':session.workdir='/foreign'
            elif fault=='native':session.native_id='foreign'
            elif fault=='policy':session.launch_spec.digest='foreign'
            elif fault=='guard':run.frozen_policy={**run.frozen_policy,'guardDigest':p.sha256_digest(b'foreign')}
            elif fault=='node':c.placement[2]=['foreign']
            elif fault=='placement':c.placement[0]='foreign'
            else:
                binding={**run.identity['binding'], 'binding_id' if fault=='binding' else 'installation': 'foreign'}
                run.identity={**run.identity,'binding':binding}
            db.commit()
    change()
    with pytest.raises(p.RelayError):c.evidence.check()


async def test_channel_only_enrolls_once_and_has_no_input_launch_or_grant_commands(original):
    c=original;e=c.evidence
    for method in ['launch','answer','stage','authority.resolve','owner.select','terminal.input','evidence.publish']:
        with pytest.raises(p.RelayError):await e.handle(method,{})
    with pytest.raises(p.RelayError):await e.handle('evidence.enroll',{'enrollment':'foreign'})
    assert (await e.handle('evidence.enroll',{'enrollment':e.token}))['runId']=='1'
    with pytest.raises(p.RelayError):await e.handle('evidence.enroll',{'enrollment':e.token})
    with pytest.raises(p.RelayError):await e.handle('evidence.publish',{'runId':'foreign'})


async def test_sweep_is_gated_bounded_and_never_constructs_a_writer(original,monkeypatch):
    from acc.server.models import RelayDecision
    from acc.server.session_relay import maintenance, reconciliation
    c=original;calls=[]
    @write_intent
    def seed():
        with c.db() as db:
            db.add(RelayDecision(session_id='relay',id='chosen',run_id=1,
                decision={'choiceId':'accept','status':'outcome_unknown'}))
            db.commit()
    seed()
    async def evidence(session_id,run_id):calls.append((session_id,run_id));return {'reconciled':0}
    ctx=SimpleNamespace(db=c.db,relay_manager=SimpleNamespace(reconcile_decisions=evidence))
    monkeypatch.setattr(reconciliation,'QUALIFIED',False)
    await maintenance.reconcile_decisions(ctx,limit=1);assert not calls
    monkeypatch.setattr(reconciliation,'QUALIFIED',True)
    await maintenance.reconcile_decisions(ctx,limit=1);assert calls==[('relay',1)]
    await maintenance.reconcile_decisions(ctx,limit=1);assert calls==[('relay',1)]
    await maintenance.reconcile_decisions(ctx,limit=1);assert calls==[('relay',1),('relay',1)]


@pytest.mark.parametrize('fault',['writeable','symlink','changed','parent_symlink'])
def test_original_frozen_guards_are_read_only_and_anchored_under_reviewed_run(tmp_path,fault):
    from acc.broker.claude_reconciliation import check_guards
    directory=tmp_path/'run';directory.mkdir()
    path=directory/'guardrails.json';path.write_text('{}');path.chmod(0o400)
    binding=p.Binding('binding','installation','node','account',str(tmp_path),'claude','native',
        p.sha256_digest(p.canonical_json({'guardrails.json':p.sha256_digest(b'{}')})))
    check_guards(directory,binding)
    if fault=='writeable':path.chmod(0o600)
    elif fault=='symlink':
        other=directory/'other';path.rename(other);path.symlink_to(other)
    elif fault=='parent_symlink':
        link=tmp_path/'link';link.symlink_to(directory,target_is_directory=True);directory=link
    else:path.chmod(0o600);path.write_text('{"foreign":true}');path.chmod(0o400)
    with pytest.raises((OSError,p.RelayError)):check_guards(directory,binding)
