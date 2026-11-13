from types import SimpleNamespace

from acc.broker.claude_owner import ClaudeOwnerFactory


async def test_auth_status_cached_but_main_authority_never_cached(monkeypatch):
    import acc.broker.claude_owner as module
    clock=[0.];calls=[];checks=[];allowed=[True]
    monkeypatch.setattr(module,'auth_clock',lambda:clock[0],raising=False)
    def probe(*args,**kwargs):calls.append(True);return 'api_key'
    monkeypatch.setattr('acc.broker.claude_auth.probe',probe)
    async def current():checks.append(True);return allowed[0]
    spec=SimpleNamespace(run_id='one',spec={'auth_mode':'api_key','terminal_resume_args':[]},binding=SimpleNamespace(workspace='/unused'))
    owner=ClaudeOwnerFactory.__new__(ClaudeOwnerFactory)
    owner.base=spec;owner.runtime=SimpleNamespace(spec=spec,controller=None,owner_authorized=current)
    owner.guard_check=lambda:None;owner.environment=lambda *_:{};owner.executable='/unused'
    for _ in range(3):assert await owner.authorize()
    assert len(calls)==1 and len(checks)>=6
    allowed[0]=False
    assert not await owner.authorize() and len(calls)==1
    allowed[0]=True;clock[0]=31
    assert await owner.authorize() and len(calls)==2
    next_spec=SimpleNamespace(run_id='two')
    assert await owner.authorize(next_spec) and len(calls)==3
