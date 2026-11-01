import {useState} from 'react';
import {Link,useLocation} from 'react-router-dom';
import {useProjects,useRole,useSessions} from '../lib/queries';
import {SECURITY_COPY,scopeName,securityLink,useSecurityPolicy,type SecurityPolicy,type SecurityScope} from '../lib/security';
import {Badge,Button,Dialog} from './ui';

export function AlwaysEnforced() {
  return <section className="space-y-2 text-sm" aria-label="Always enforced">
    <p>Authentication, owner approvals, vault access rules, OS permissions, machine isolation and audit remain enforced.</p>
    <details><summary className="cursor-pointer font-medium">Always enforced — details</summary>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted">
        <li>Sign-in, reauthentication, roles, project ownership, CSRF and origin checks.</li>
        <li>Broker identity, machine/account routing, unprivileged accounts and protected launch/hook files.</li>
        <li>Read-only and Prepare assignments; team grants, ownership, depth, expiry, stops and gate authority.</li>
        <li>Local-only workspace consent, repository trust and safe file, archive, Git metadata and payload validation.</li>
        <li>Owner tool/system-package approvals, privileged helper restrictions and protected system/service paths.</li>
        <li>Publication/DNS approval, owned ports, preview authentication and issue-server/CA allowlists.</li>
        <li>Vault encryption, key grants, reserved variables, credential redaction and remote-delivery consent.</li>
        <li>Container qualification, mounts, limits and egress; WSL isolation and macOS privacy.</li>
        <li>Provider quotas, OS resource limits, gate host opt-in and branch-code trust approval.</li>
        <li>Auditing, safe errors, update signatures, approved sources and scoped unsigned exceptions.</li>
        <li>Backup ownership, receiver authentication, encryption and recovery checks.</li>
        <li>Public-device pairing, mobile credential lifecycle, telemetry/export consent and feature availability.</li>
      </ul>
    </details>
  </section>;
}

export function SecurityBadge({policy,label,frozen=false,older=false}: {policy?:SecurityPolicy;label?:string;frozen?:boolean;older?:boolean}) {
  const [open,setOpen]=useState(false);
  const owner=useRole().owner;
  if(!policy)return older?<Badge tone="wait" className="h-auto min-h-5 min-w-0 max-w-full whitespace-normal leading-normal [overflow-wrap:anywhere]">{label??'This session'} · older settings · frozen protections unavailable</Badge>:null;
  const off=Object.entries(policy.switches).filter(([,enabled])=>enabled===false);
  const pending=policy.enforcement?.allowed_folders==='pending';
  const varies=policy.varying_switches?.length;
  const olderCount=policy.older_sessions?.length??0;
  if(!off.length&&!pending&&!varies&&!older&&!olderCount)return null;
  const unrestricted=policy.mode==='unrestricted'&&!pending;
  const base=unrestricted?'Unrestricted':!off.length&&pending?'Folder protection pending':!off.length&&varies?'Security varies by person':!off.length?(frozen?'Frozen protections On':'Current protections On'):`Custom security · ${off.length} off${varies?' · varies by person':''}`;
  const words=`${base}${pending&&off.length?' · folders pending':''}${olderCount?` · ${olderCount} ${olderCount===1?'session':'sessions'} on older settings`:''}${older?' · older settings':''}`;
  const scope=label??scopeName(policy.scope);
  return <>
    <button type="button" onClick={()=>setOpen(true)} className="flex min-h-8 min-w-0 max-w-full items-center text-left pointer-coarse:min-h-11" aria-label={`${words} · ${scope}${frozen?' · frozen launch':''}`}>
      <Badge tone="wait" className="h-auto max-w-full whitespace-normal py-1 [overflow-wrap:anywhere]"><span className="[overflow-wrap:anywhere]">{words} · {scope}</span></Badge>
    </button>
    <Dialog open={open} onClose={()=>setOpen(false)} title="Security protections" footer={<Button onClick={()=>setOpen(false)}>Close</Button>}>
      <div className="space-y-3 text-sm">
        <p>{frozen?'Frozen launch':'Current policy'} revision {policy.revision} · {scope}. {frozen?'Later settings do not relabel this running generation.':'New launches use the reviewed policy. Native launch protections stay frozen; folder reloads change later account-service operations for running sessions.'}</p>
        {older&&<p className="text-wait">This session is on older, looser settings. Stop it and start a new session to use current protections; resuming this generation is refused.</p>}
        {!!olderCount&&<p className="text-wait">{olderCount} {olderCount===1?'session':'sessions'} on older settings across this instance. Open Security settings to review their confirmed Stop actions.</p>}
        {pending&&<p className="text-wait">Pending — sessions may still have earlier access. The machine has not acknowledged the folder policy.</p>}
        {varies&&<p>Saved person-specific tool exceptions differ on this account. Inspect a project for its execution owner's policy.</p>}
        <ul className="space-y-2">{off.map(([id])=><li key={id}>{SECURITY_COPY[id]?.label??id}: Off. {SECURITY_COPY[id]?.off}</li>)}</ul>
        <AlwaysEnforced/>
        <Link className="underline" to={owner?securityLink(policy.scope):'/settings#security'} onClick={()=>setOpen(false)}>{owner?'Open Security settings':'An owner manages these settings'}</Link>
      </div>
    </Dialog>
  </>;
}

/** Current execution account, separate from the frozen session badge. Members never query account policy. */
export function CurrentSecurityBadge() {
  const role=useRole(), location=useLocation();
  const projects=useProjects(), sessions=useSessions();
  const [,section,id]=location.pathname.split('/');
  const session=section==='sessions'?sessions.data?.find(s=>s.id===id):undefined;
  const params=new URLSearchParams(location.search);
  const settingsProject=section==='settings'&&params.get('security_scope')==='project'?Number(params.get('project_id')):undefined;
  const project=projects.data?.find(p=>section==='projects'?String(p.id)===id:p.id===(settingsProject??session?.project_id));
  const editingAccount=section==='settings'&&params.get('security_scope')==='account';
  const scope:SecurityScope|undefined=role.owner&&role.account?{kind:'account',
    machine:editingAccount?(params.get('machine')??'local'):session?.host??project?.host??'local',
    account:editingAccount?(params.get('account')??role.account):session?.run_as??project?.account??role.account}:undefined;
  const instancePolicy=useSecurityPolicy(role.owner?{kind:'instance'}:undefined);
  const accountPolicy=useSecurityPolicy(scope);
  const projectPolicy=useSecurityPolicy(role.owner&&project?{kind:'project',project_id:project.id}:undefined);
  if(!role.owner)return null;
  const projectRelaxed=!!projectPolicy.data&&(Object.values(projectPolicy.data.switches).some(value=>!value)||!!projectPolicy.data.older_sessions?.length);
  const selected=projectRelaxed?projectPolicy.data:accountPolicy.data;
  const policy=selected??instancePolicy.data;
  const combined=policy?{...policy,older_sessions:instancePolicy.data?.older_sessions??[]}:undefined;
  return <div className="min-w-0 space-y-1"><SecurityBadge policy={combined} label={projectRelaxed?`${project!.name} project`:scope?scopeName(scope):'Instance defaults'}/>
    {(instancePolicy.error||!instancePolicy.data)&&<span className="text-xs text-wait">Older-session inventory {instancePolicy.error?'unavailable':'loading'}{instancePolicy.data?' · last known count shown':''}.</span>}
    {(accountPolicy.error||projectPolicy.error)&&<span className="text-xs text-wait">Security state unavailable{policy?' · last known policy shown':''}.</span>}
  </div>;
}
