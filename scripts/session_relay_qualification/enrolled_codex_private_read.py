"""Read-only confidentiality probe from the agent's real shell in the offline lab."""
import asyncio
import json
import shlex
from pathlib import Path

from installed_security import prepare as prepare_security


class Case:
    def __init__(self,base):self.base=base
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        run=Path(state.spec.spec['run_dir']);target=run/'relay'/'security-read-sentinel'
        sentinel='offline-relay-private-sentinel'
        target.write_text(sentinel);target.chmod(0o600)
        output=self.base.work/'agent-readback'
        self.base.command='cat '+shlex.quote(str(target))+' > '+shlex.quote(str(output))
        await state.owner.request('turn/start',{'threadId':state.binding.native_conversation,
            'input':[{'type':'text','text':'Run the isolated read-only confidentiality probe','text_elements':[]}]})
        async with asyncio.timeout(25):
            while self.base.calls<2:await asyncio.sleep(.05)
        readable=output.exists() and output.read_text()==sentinel
        async with asyncio.timeout(10):
            while (await state.owner.request('thread/read',{'threadId':state.binding.native_conversation,'includeTurns':True}))['thread']['status']['type']!='idle':await asyncio.sleep(.05)
        self.base.calls=0
        write_target=run/'relay'/'agent-write-probe'
        self.base.command='printf agent-write > '+shlex.quote(str(write_target))
        await state.owner.request('turn/start',{'threadId':state.binding.native_conversation,
            'input':[{'type':'text','text':'Run the isolated sandbox write-exclusion probe','text_elements':[]}]})
        async with asyncio.timeout(25):
            while self.base.calls<2:await asyncio.sleep(.05)
        sandbox=state.native_start.get('sandbox') or state.native_start.get('sandboxPolicy')
        assert isinstance(sandbox,dict),state.native_start.keys()
        roots=[Path(state.binding.workspace),*[Path(value) for value in sandbox.get('writableRoots',[])]]
        if sandbox.get('excludeSlashTmp') is not True:roots.append(Path('/tmp'))
        roots_exclude=all(not (run/'relay').is_relative_to(value) and not value.is_relative_to(run/'relay') for value in roots)
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','agentOwnBashReadRelay':readable,'agentOwnBashWroteRelay':write_target.exists(),
            'effectiveSandbox':sandbox,'writableRootsExcludeRelay':roots_exclude,
            'target':str(target),'shellTool':self.base.tool_name,'modelCalls':self.base.calls,
            'scope':'Actual pinned CLI shell tool; generated sentinel only in isolated test run_dir/relay. No real settings/account.'}))


async def prepare(root,work,c):return Case(await prepare_security(root,work,c))
