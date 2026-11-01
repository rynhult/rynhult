import {useEffect,useRef,useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {Link,useLocation,useNavigate} from 'react-router-dom';
import {api,ApiError,post} from '../lib/api';
import {useNewSession} from './newSession';
import {SecurityVerification} from './SecurityVerification';
import {useOffline,useProjects,useRole,useSessions} from '../lib/queries';
import {SECURITY_COPY,securityAuditLabel,scopeEqual,scopeName,securityLink,useSecurityPolicy,type SecurityRegistry,type SecurityScope} from '../lib/security';
import type {Project} from '../lib/types';
import {useConfirm} from './confirm';
import {AlwaysEnforced,SecurityBadge} from './SecurityBadge';
import {announce,Badge,Button,Card,CardHeader,Dialog,ErrorNote,Field,QueryState,Select} from './ui';

type Preset='custom'|'unrestricted'|'restore';
interface Draft {revision:number;scope:SecurityScope;initial:Record<string,boolean>;overrides:Record<string,boolean>;preset:Preset;choice:string}
interface Preview {id:string;digest:string;before:Record<string,boolean>;after:Record<string,boolean>;scope:SecurityScope;revision:number;expires_at:string;active_sessions:string[];older_sessions?:string[]}
const changed=(draft:Draft)=>draft.preset!=='custom'||JSON.stringify(draft.initial)!==JSON.stringify(draft.overrides);
const word=(value:boolean|null|undefined)=>value===undefined?'Inherited':value===null?'Varies by person':value?'On':'Off';


export function SecuritySettings({owner}: {owner:boolean}) {
  const location=useLocation(),navigate=useNavigate(),role=useRole();
  const sharedProjects=useProjects();
  // A role change must not reuse a cached owner's wider project list in the member selector.
  const memberProjects=useQuery({queryKey:['security-member-projects',role.username,role.role],
    queryFn:()=>api<Project[]>('/api/projects'),enabled:!owner&&role.known,refetchOnMount:'always'});
  const projects=owner?sharedProjects:memberProjects;
  const registry=useQuery({queryKey:['security-scopes'],queryFn:()=>api<SecurityRegistry>('/api/security/scopes'),enabled:owner,refetchInterval:10000});
  const nodes=useQuery({queryKey:['nodes'],queryFn:()=>api<{name:string;id:string;status:string;accounts:string[]}[]>('/api/nodes'),enabled:owner});
  const people=useQuery({queryKey:['security-people'],queryFn:()=>api<{users:{account?:string}[]}>('/api/users'),enabled:owner});
  const params=new URLSearchParams(location.search);
  const kind=owner?(params.get('security_scope')??'instance'):'project';
  const selectedProject=projects.data?.find(p=>p.id===Number(params.get('project_id')))??(!owner?projects.data?.[0]:undefined);
  const scope:SecurityScope=kind==='project'?{kind:'project',project_id:selectedProject?.id??Number(params.get('project_id'))}:
    kind==='account'?{kind:'account',machine:params.get('machine')??'local',account:params.get('account')??role.account??''}:{kind:'instance'};
  const accounts=new Map<string,{machine:string;account:string}>();
  const add=(machine:string,account:string|undefined)=>{if(account&&!['root','acc','acc-node'].includes(account))accounts.set(JSON.stringify([machine,account]),{machine,account});};
  if(owner){add('local',role.account);people.data?.users.forEach(p=>add('local',p.account));projects.data?.forEach(p=>add(p.host,p.account));nodes.data?.filter(n=>n.status==='active').forEach(n=>n.accounts.forEach(a=>add(n.name,a)));}
  const options=[...accounts.entries()];
  const cache=useRef(new Map<string,Draft>());
  const [dirty,setDirty]=useState(false);
  useEffect(()=>{if(!owner){cache.current.clear();setDirty(false);}},[owner]);
  const confirm=useConfirm();
  const choose=async(next:SecurityScope)=>{
    if(dirty&&!await confirm({title:'Change Security scope?',body:'Your unsaved draft stays in this tab. It has not changed the effective policy.',confirm:'Change scope'}))return;
    navigate(securityLink(next));
  };
  const projectList=(projects.data??[]).filter(p=>scope.kind!=='account'||p.host===scope.machine&&p.account===scope.account);
  const key=JSON.stringify(scope);
  return <div className="space-y-4 min-w-0 [overflow-wrap:anywhere]">
    <Card><CardHeader title="Security" subtitle="Owner-managed guardrails, separate from authentication, authority and OS permissions."/>
      <div className="space-y-3 p-4">
        {!owner&&<p className="text-sm">An owner manages these settings. <Link className="underline" to="/guide/User-Guide/Guardrails#ask-an-owner">Ask an owner</Link>.</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          {owner&&<Field label="Security scope"><Select value={scope.kind} onChange={e=>{
            const next=e.target.value;
            if(next==='instance')void choose({kind:'instance'});
            if(next==='account'&&options[0])void choose({kind:'account',...options[0][1]});
            if(next==='project'&&projects.data?.[0])void choose({kind:'project',project_id:projects.data[0].id});
          }}><option value="instance">Instance defaults</option><option value="account" disabled={!options.length}>Machine / agent account</option><option value="project" disabled={!projects.data?.length}>Project</option></Select></Field>}
          {owner&&scope.kind==='account'&&<Field label="Machine and agent account"><Select value={JSON.stringify([scope.machine,scope.account])} onChange={e=>{const next=accounts.get(e.target.value);if(next)void choose({kind:'account',...next});}}>
            {options.map(([value,a])=><option key={value} value={value}>{a.machine} / {a.account}</option>)}
          </Select></Field>}
          {scope.kind==='project'&&<Field label="Project security"><Select value={scope.project_id||''} onChange={e=>void choose({kind:'project',project_id:Number(e.target.value)})}>
            <option disabled value="">Choose a project</option>{projectList.map(p=><option key={p.id} value={p.id}>{owner?`${p.name} · ${p.host} / ${p.account}`:p.name}</option>)}
          </Select></Field>}
        </div>
        <ErrorNote error={projects.error}/>{owner&&<><ErrorNote error={nodes.error}/><ErrorNote error={people.error}/></>}
      </div>
    </Card>
    {owner?<QueryState query={registry}>{data=><SecurityEditor key={key} scope={scope} registry={data} refreshRegistry={()=>registry.refetch()} projects={projects.data??[]} projectsReady={!!projects.data&&!projects.error} cache={cache.current} onDirty={setDirty}/>}</QueryState>:
      !projects.data?<p className="text-sm text-muted">{projects.error?'Your project list is unavailable. Retry before choosing a scope.':'Loading your project access…'}</p>:selectedProject?<MemberSecurity scope={{kind:'project',project_id:selectedProject.id}}/>:<p className="text-sm text-muted">Choose one of your projects to see its effective protections. Other accounts and private policy metadata are not available here.</p>}
  </div>;
}

function MemberSecurity({scope}:{scope:SecurityScope}) {
  const query=useSecurityPolicy(scope);
  return <QueryState query={query}>{data=><Card><CardHeader title="Effective protections" subtitle={`Project policy · revision ${data.revision}`}/><div className="space-y-3 p-4">
    <SecurityBadge policy={data}/><ul className="space-y-2">{Object.entries(data.switches).map(([id,on])=><li key={id} className="flex flex-wrap items-center justify-between gap-2 text-sm">{SECURITY_COPY[id]?.label??id}<Badge>{word(on)}</Badge></li>)}</ul>
    {data.enforcement?.allowed_folders==='pending'&&<p className="text-sm text-wait">Pending — sessions may still have earlier access. The machine has not acknowledged the folder policy.</p>}
    <AlwaysEnforced/>
  </div></Card>}</QueryState>;
}

function SecurityEditor({scope,registry,refreshRegistry,projects,projectsReady,cache,onDirty}: {
  scope:SecurityScope;registry:SecurityRegistry;refreshRegistry:()=>Promise<unknown>;projects:Project[];projectsReady:boolean;cache:Map<string,Draft>;onDirty:(dirty:boolean)=>void;
}) {
  const confirm=useConfirm(),newSession=useNewSession();
  const key=JSON.stringify(scope), query=useSecurityPolicy(scope), client=useQueryClient(), sessions=useSessions(), offline=useOffline();
  const own=registry.policies.find(p=>scopeEqual(p.scope,query.data?.scope??scope));
  const [draft,setDraft]=useState<Draft|undefined>(()=>cache.get(key));
  const [review,setReview]=useState<Preview|null>(null),[verify,setVerify]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null),[saved,setSaved]=useState(false),[folderApplication,setFolderApplication]=useState<string|null>(null),[expired,setExpired]=useState(false);
  const reviewButton=useRef<HTMLSpanElement>(null);
  const effectiveStatus=useRef<HTMLDivElement>(null);
  const dirty=!!draft&&changed(draft);
  const confirmFolderRecovery=!!query.data?.can_confirm_folder_recovery&&!dirty;
  const changedElsewhere=!!draft&&(draft.revision!==registry.revision||!!query.data&&!scopeEqual(draft.scope,query.data.scope));
  const unknown=registry.registry.some(r=>!SECURITY_COPY[r.id]);
  const stale=!!query.error||!query.data||query.data.revision!==registry.revision;
  const blocked=busy||offline||stale||changedElsewhere||unknown||!projectsReady;
  const adopt=()=>{
    if(!query.data||query.data.revision!==registry.revision)return;
    const overrides=own?.overrides??{};
    setDraft({revision:registry.revision,scope:query.data.scope,initial:overrides,overrides,preset:'custom',choice:Object.keys(overrides).length?'custom':'default'});setError(null);
  };
  useEffect(()=>{if(!dirty&&!busy)adopt();},[query.data,registry,dirty,busy]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{if(draft)cache.set(key,draft);onDirty(dirty);},[draft,key,cache,dirty,onDirty]);
  useEffect(()=>{
    if(!dirty)return;
    const warn=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue='';};
    window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);
  },[dirty]);
  useEffect(()=>{
    const check=()=>setExpired(!!review&&Date.parse(review.expires_at)<=Date.now());check();
    const timer=review?setTimeout(check,Math.max(0,Date.parse(review.expires_at)-Date.now())):undefined;
    window.addEventListener('focus',check);return()=>{clearTimeout(timer);window.removeEventListener('focus',check);};
  },[review]);
  const relevantProjects=projects.filter(p=>scope.kind==='instance'||scope.kind==='project'&&p.id===scope.project_id||scope.kind==='account'&&p.host===scope.machine&&p.account===scope.account);
  const older=(query.data?.older_sessions??[]).map(id=>({id,session:sessions.data?.find(s=>s.id===id)}));
  const unconfirmed=(query.data?.unconfirmed_stops??[]).filter(id=>!query.data?.older_sessions?.includes(id)).map(id=>({id,session:sessions.data?.find(s=>s.id===id)}));
  const machineGone=async(id:string,machine:string)=>{
    if(!await confirm({title:`Confirm ${machine} is gone?`,body:'Only use this after removing the machine and independently confirming it will not run this session again. This clears an unconfirmed-stop record; it does not prove a stop or kill anything.',confirm:'Machine is gone',danger:true}))return;
    setBusy(true);setError(null);
    try{await post(`/api/security/sessions/${id}/machine-gone`,{confirm_machine:machine});await Promise.all([client.invalidateQueries({queryKey:['sessions']}),client.invalidateQueries({queryKey:['security-effective']})]);announce('Unconfirmed stop record cleared after owner confirmation. No machine stop was proven.');requestAnimationFrame(()=>effectiveStatus.current?.focus());}
    catch(e){setError(e);}finally{setBusy(false);}
  };
  const stopOlder=async(id:string,name:string)=>{
    if(!await confirm({title:`Stop ${name}?`,body:'This stops the older generation. Wait for the machine to confirm the stop. Starting a new session re-validates current Security settings; resuming the old weaker generation is refused.',confirm:'Stop',danger:true}))return;
    setBusy(true);setError(null);
    try{await post(`/api/sessions/${id}/stop`,{});await Promise.all([client.invalidateQueries({queryKey:['sessions']}),client.invalidateQueries({queryKey:['security-effective']})]);}
    catch(e){setError(e);}finally{setBusy(false);}
  };
  const descendants=registry.policies.filter(p=>!scopeEqual(p.scope,scope)&&(scope.kind==='instance'||scope.kind==='account'&&p.scope.kind==='project'&&relevantProjects.some(x=>p.scope.kind==='project'&&x.id===p.scope.project_id)));
  const audit=useQuery({queryKey:['security-audit'],queryFn:()=>api<{items:{id:number;actor:string;action:string;ts:string}[]}>('/api/audit?action=security.policy.&page=true&limit=10'),refetchInterval:10000});
  const edit=(overrides:Record<string,boolean>,choice='custom',preset:Preset='custom')=>{if(!draft||busy)return;setDraft({...draft,overrides,choice,preset});setSaved(false);setError(null);};
  const preview=async()=>{
    if(!draft||blocked)return;setBusy(true);setError(null);
    try{setReview(await post<Preview>('/api/security/changes/preview',{scope:draft.scope,overrides:draft.overrides,preset:draft.preset,revision:draft.revision}));}
    catch(e){setError(e);if(e instanceof ApiError&&e.status===403)setVerify(true);}
    finally{setBusy(false);}
  };
  const closeReview=()=>{if(busy)return;setReview(null);requestAnimationFrame(()=>reviewButton.current?.querySelector('button')?.focus());};
  const apply=async()=>{
    if(!review||blocked||expired)return;setBusy(true);setError(null);
    try {
      const result=await post<{folder_application?:{state:string}|null}>(`/api/security/changes/${review.id}/apply`,{digest:review.digest});
      setFolderApplication(result.folder_application?.state??null);
      cache.delete(key);setDraft(undefined);setReview(null);setSaved(true);
      await Promise.all([client.invalidateQueries({queryKey:['security-effective']}),client.invalidateQueries({queryKey:['security-scopes']}),client.invalidateQueries({queryKey:['guardrails']}),client.invalidateQueries({queryKey:['security-audit']})]);
      announce('Security policy saved. Native launch protections stay frozen; folder reloads change later account-service access.');
      requestAnimationFrame(()=>effectiveStatus.current?.focus());
    } catch(e){setError(e);if(e instanceof ApiError&&e.status===409)void refreshRegistry();if(e instanceof ApiError&&e.status===403){setReview(null);setVerify(true);}}
    finally{setBusy(false);}
  };
  return <QueryState query={query}>{effective=><div className="space-y-4">
    <div ref={effectiveStatus} role="region" aria-label="Effective Security status" tabIndex={-1}><Card><CardHeader title="Effective now" subtitle={`${scopeName(effective.scope)} · revision ${effective.revision} · checked ${new Date(query.dataUpdatedAt).toLocaleTimeString()}`}/>
      <div className="space-y-3 p-4 text-sm">
        <p>Effective mode: <strong>{effective.mode==='unrestricted'?'Unrestricted':effective.mode==='custom'?'Custom':'Default'}</strong>. Default inherits its parent; it does not reset inherited choices to On.</p>
        <SecurityBadge policy={effective}/>
        {effective.enforcement?.allowed_folders==='pending'&&<p className="text-wait">Pending — sessions may still have earlier access. Folder policy has not been acknowledged by the machine; this is not an applied Unrestricted scope.</p>}
        {draft&&<Field label="Security mode"><Select value={draft.choice} disabled={busy||offline} onChange={e=>{
          const choice=e.target.value;
          edit(choice==='default'?{}:choice==='unrestricted'?Object.fromEntries(registry.registry.filter(r=>scope.kind==='account'||!r.account_only).map(r=>[r.id,false])):draft.overrides,choice,choice==='unrestricted'?'unrestricted':'custom');
        }}><option value="default">Default — inherit</option><option value="custom">Custom</option><option value="unrestricted">Unrestricted — review every switch</option></Select></Field>}
        {own?.can_restore&&<Button disabled={busy||offline} onClick={()=>draft&&edit(draft.overrides,'custom','restore')}>Restore previous settings</Button>}
        {saved&&<p role="status">Server policy saved. Native launch protections stay frozen. Folder reloads change later account-service operations for running sessions.</p>}
        {changedElsewhere&&<p role="alert" className="text-wait">Security changed elsewhere. Your draft is kept, but its old revision cannot be applied. <Button onClick={adopt} disabled={busy||stale}>Discard draft and reload</Button></p>}
        {unknown&&<ErrorNote error="This server has new protections. Update this UI before changing Security; no new switch will be silently disabled."/>}
        {stale&&<p className="text-wait">Security state is stale or unavailable. Changes are disabled until it refreshes.</p>}
        <ErrorNote error={error} action={<Button size="sm" disabled={busy||offline} onClick={()=>{void query.refetch();void refreshRegistry();}}>Refresh current state</Button>}/>
      </div>
    </Card></div>
    {saved&&folderApplication&&<p role="status">Folder policy: {folderApplication==='applied'?'acknowledged by the target':'pending target acknowledgement — new work stays blocked'}. <Link className="underline" to={scope.kind==='account'?`/settings/security/folders?machine=${encodeURIComponent(scope.machine)}&account=${encodeURIComponent(scope.account)}`:'/settings/security/folders'}>Review or retry in Allowed folders</Link>.</p>}
    <Card><CardHeader title="Agent guardrails" subtitle="Effective values and their source are shown separately from your unsaved scope overrides."/>
      <ul className="divide-y divide-line px-4">{registry.registry.map(row=>{
        const inherited=draft?.overrides[row.id]===undefined;
        const locked=row.account_only&&scope.kind!=='account';
        const source=effective.sources?.[row.id];
        const sourceLabel=source?.kind==='varies'?'Legacy person-specific settings':source?.kind==='default'?'Built-in default':source?.kind==='legacy'?'Previous owner configuration':source?scopeName(source):'Source unavailable';
        return <li key={row.id} className="space-y-2 py-3 text-sm">
          <div className="flex items-start justify-between gap-3"><label htmlFor={`security-${row.id}`} className="font-medium">{row.label}</label>
            {effective.switches[row.id]===null?<Select id={`security-${row.id}`} aria-label={`${row.label} override`} value={inherited?'inherit':draft?.overrides[row.id]?'on':'off'} disabled={locked||busy||offline||unknown}
              onChange={e=>{if(draft){const next={...draft.overrides};if(e.target.value==='inherit')delete next[row.id];else next[row.id]=e.target.value==='on';edit(next);}}}>
              <option value="inherit">Inherited — varies by person</option><option value="on">On for this scope</option><option value="off">Off for this scope</option>
            </Select>:<input id={`security-${row.id}`} type="checkbox" role="switch" checked={draft?.overrides[row.id]??effective.switches[row.id]??false} disabled={locked||busy||offline||unknown}
              aria-describedby={`security-help-${row.id}`} className="mt-1 size-5 shrink-0 accent-[var(--accent)] pointer-coarse:size-6" onChange={e=>draft&&edit({...draft.overrides,[row.id]:e.target.checked})}/>}
          </div>
          <p className="text-xs text-muted">{row.id==='allowed_folders'&&effective.enforcement?.allowed_folders==='pending'?'Desired:':'Effective now:'} {word(effective.switches[row.id])} · {sourceLabel}. Draft: {inherited?'Inherited':word(draft?.overrides[row.id])}.</p>
          {row.id==='allowed_folders'&&effective.enforcement?.allowed_folders==='pending'&&<p className="text-xs text-wait">Pending target acknowledgement. The desired policy is not confirmed on this target yet.</p>}
          {effective.switches[row.id]===null&&<p className="text-xs text-muted">Saved person-specific exceptions differ. Inspect a project for its execution owner's policy, or review an explicit override for everyone on this account.</p>}
          <p id={`security-help-${row.id}`} className="text-xs text-muted">Off: {SECURITY_COPY[row.id]?.off??'New protection; update this UI before changing it.'}</p>
          {row.id==='risk_labels'&&<p className="text-xs text-muted">Display also requires the Admin feature and the viewer's personal preference. On here cannot enable an unavailable feature.</p>}
          {(row.id==='budget_checks'||row.id==='custom_rules')&&<p className="text-xs text-muted">On enforces saved rules; it does not create any rules.</p>}
          {locked?<p className="text-xs text-muted">Machine/account setting — not editable at this scope.</p>:
            <Button size="sm" variant="ghost" aria-label={`Use inherited setting for ${row.label}`} disabled={inherited||busy||offline} onClick={()=>{if(draft){const next={...draft.overrides};delete next[row.id];edit(next);}}}>Use inherited setting</Button>}
        </li>;
      })}</ul>
    </Card>
    <Card><CardHeader title="Allowed folders" subtitle="Folder policy and filesystem access are separate."/><div id="security-allowed-folders" className="space-y-2 p-4 text-sm">
      <p>Limits the folders Rynhult's account service will operate on. It does not sandbox native agent processes.</p>
      <Link className="underline" to={scope.kind==='account'?`/settings/security/folders?machine=${encodeURIComponent(scope.machine)}&account=${encodeURIComponent(scope.account)}`:'/settings/security/folders'}>Allowed folders</Link>
      {scope.kind==='account'?<><p>{scopeName(effective.scope)} · revision {effective.revision} · {effective.enforcement?.allowed_folders==='pending'?'Pending application':'No folder-policy change pending'}.</p><p className="text-muted">No OS access is granted by editing folder policy. Saved roots are kept; privileged grant/undo restrictions always apply.</p></>:
        <p>Choose Machine / agent account above to edit this switch. Projects inherit it and cannot override it.</p>}
      {scope.kind==='project'&&relevantProjects[0]?.account&&<Link className="underline" to={securityLink({kind:'account',machine:relevantProjects[0].host,account:relevantProjects[0].account})}>Open this account's Allowed folders settings</Link>}
    </div></Card>
    {scope.kind==='instance'&&<SavedSecurityPatterns/>}
    <Card><CardHeader title="Always enforced"/><div className="p-4"><AlwaysEnforced/></div></Card>
    <Card><CardHeader title="Sessions on older settings" subtitle="Saved policy applies immediately to new launches. Native launch protections remain frozen; folder reloads change later account-service access for running sessions."/><div className="space-y-2 p-4 text-sm">
      <p>{older.length} {older.length===1?'session':'sessions'} on older settings. These sessions keep older, looser protections until stopped and replaced. Resuming a weaker generation is refused.</p>
      <p>Automatic restart is not supported here: provider conversations and team grants cannot be safely replayed. Stop, wait for confirmation, then start a new session and review its launch choices under current policy.</p>
      <ul className="space-y-3">{older.map(({id,session:s})=><li key={id} className="flex flex-wrap items-center gap-2">
        <Link className="inline-flex min-h-11 items-center underline" to={`/sessions/${id}`}>{s?.name??id}</Link>
        <span>{s?.observed_state??'Checking session status'}{s?.machine_offline?' · machine offline':''}{s?.last_error?.startsWith('stop not confirmed:')?' · stop not confirmed':''}</span>
        <Button disabled={busy||offline} onClick={()=>void stopOlder(id,s?.name??id)}>Stop</Button>
        {s?.last_error?.startsWith('stop not confirmed:')&&s.host&&s.host!=='local'&&<Button disabled={busy||offline} onClick={()=>void machineGone(id,s.host??'local')}>Machine is gone</Button>}
        <Button disabled={busy||offline} onClick={()=>newSession(s?.project_id?{mode:'project',project_id:s.project_id}:undefined)}>Start a new session</Button>
      </li>)}</ul>
      <ErrorNote error={sessions.error}/>
    </div></Card>
    {!!unconfirmed.length&&<Card><CardHeader title="Unconfirmed stops" subtitle="An uncertain machine stop is not proof that its process ended."/><div className="space-y-2 p-4 text-sm"><ul>{unconfirmed.map(({id,session:s})=><li key={id} className="flex flex-wrap items-center gap-2">
      <Link className="inline-flex min-h-11 items-center underline" to={`/sessions/${id}`}>{s?.name??id}</Link>
      <Button disabled={busy||offline} onClick={()=>void stopOlder(id,s?.name??id)}>Stop</Button>
      {s&&s.host&&s.host!=='local'&&<Button disabled={busy||offline} onClick={()=>void machineGone(id,s.host??'local')}>Machine is gone</Button>}
    </li>)}</ul></div></Card>}
    <Card><CardHeader title="Recent Security audit" subtitle="Recent instance policy reviews and changes; not a machine-application acknowledgement."/><div className="space-y-2 p-4 text-xs">
      <QueryState query={audit}>{data=>data.items.length?<ul className="space-y-1">{data.items.map(r=><li key={r.id}>{new Date(r.ts).toLocaleString()} · {r.actor} · {securityAuditLabel(r.action)}</li>)}</ul>:<p>No recent Security events.</p>}</QueryState>
      <Link className="underline" to="/audit">Open audit log</Link>
    </div></Card>
    {(dirty||confirmFolderRecovery)&&<div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-panel p-3 text-sm">
      <span>{confirmFolderRecovery?'Acknowledged folder policy awaits confirmation':'Unsaved Security changes'} · {scopeName(scope)}</span><span ref={reviewButton}><Button variant="primary" disabled={blocked||!draft} onClick={()=>void preview()}>{confirmFolderRecovery?'Review acknowledged folder policy':'Review changes'}</Button></span>
    </div>}
    <SecurityVerification open={verify} onClose={()=>setVerify(false)} onVerified={()=>{setVerify(false);void preview();}}/>
    <Dialog open={!!review} onClose={closeReview} title="Review Security changes" dismissible={false} wide
      footer={<><Button disabled={busy} onClick={closeReview}>Cancel</Button><Button variant="danger" disabled={blocked||expired||!review||error instanceof ApiError&&error.status===409} onClick={()=>void apply()}>{busy?'Applying…':draft?.preset==='unrestricted'?'Apply Unrestricted':draft?.preset==='restore'?'Restore previous settings':'Apply Security changes'}</Button></>}>
      {review&&<div className="space-y-4 text-sm [overflow-wrap:anywhere]">
        <p className="font-medium">{draft?.preset==='unrestricted'?`Turn off all configurable guardrails for ${scopeName(review.scope)}?`:`Change Security for ${scopeName(review.scope)}?`}</p>
        {draft?.preset==='unrestricted'&&<p>Agents may read accessible credentials, modify or delete accessible files, change account tools and push or merge using their own credentials without these checks. This can affect every project listed below.</p>}
        <AlwaysEnforced/>
        {confirmFolderRecovery&&<p>The target already acknowledged this folder switch. Confirming these unchanged settings clears only the recovered admission fence; it does not reload roots or change protections.</p>}
        <p>Existing sessions will keep their current protections until you explicitly replace or restart them with the reviewed policy.</p>
        {scope.kind!=='account'&&<p className="text-wait">{effective.switches.allowed_folders?'Custom — account folder check still on. This scope cannot turn it off.':'Allowed folders is inherited from the account and cannot be changed here.'} Child overrides are kept; this is not an instance-wide all-off claim.</p>}
        {scope.kind==='account'&&<p className="text-wait">Folder changes in either direction stay pending until the target acknowledges them. Saving does not grant filesystem access or apply an OS change.</p>}
        <p>Provider prompts Off is supported only by {registry.supported_bypass_providers.join(', ')}. Other providers refuse the bypass; mandatory read-only assignments retain their restrictions.</p>
        <ul className="space-y-3">{registry.registry.map(r=><li key={r.id}><strong>{r.label}</strong><span className="block text-xs">{word(review.before[r.id])} → {word(review.after[r.id])}</span>{review.after[r.id]===false&&<p className="text-xs text-muted">{SECURITY_COPY[r.id]?.off}</p>}</li>)}</ul>
        <section aria-label="Affected projects"><h3 className="font-medium">Affected projects</h3>{relevantProjects.length?<ul>{relevantProjects.map(p=><li key={p.id}>{p.name} · {p.host} / {p.account}</li>)}</ul>:<p>No registered projects in this scope. Future projects may inherit these settings.</p>}</section>
        <section aria-label="Child overrides"><h3 className="font-medium">Child overrides are kept</h3>{descendants.length?<ul>{descendants.map((p,i)=><li key={i}>{scopeName(p.scope)}: {Object.entries(p.overrides).map(([id,on])=>`${SECURITY_COPY[id]?.label??id} ${word(on)}`).join('; ')}</li>)}</ul>:<p>No configured child overrides.</p>}</section>
        <p className="text-wait">Folder reloads affect running sessions: Off widens later account-service access and On restricts it. Native launch instructions and OS access remain unchanged.</p>
        <p>{review.older_sessions?.length??0} running {review.older_sessions?.length===1?'session keeps its':'sessions keep their'} older, looser settings until restarted. Applying is allowed; no automatic restart is performed.</p>
        {!!review.older_sessions?.length&&<ul>{review.older_sessions.map(id=><li key={id}><Link className="inline-flex min-h-11 items-center underline" to={`/sessions/${id}`}>{sessions.data?.find(s=>s.id===id)?.name??id}</Link></li>)}</ul>}
        <p className="text-xs text-muted">Revision {review.revision} · preview expires {new Date(review.expires_at).toLocaleTimeString()}. Inherited entries are resolved by the server; the diff above lists this scope's overrides.</p>
        {expired&&<p role="alert" className="text-wait">This preview expired. Cancel and review again.</p>}
        <ErrorNote error={error}/>
      </div>}
    </Dialog>
  </div>}</QueryState>;
}


function SavedSecurityPatterns() {
  const query=useQuery({queryKey:['guardrails'],queryFn:()=>api<{custom:{pattern:string;reason:string}[]}>('/api/guardrails')});
  return <Card><CardHeader title="Custom command patterns" subtitle="Saved instance patterns are kept when enforcement is Off."/><div className="space-y-3 p-4 text-sm">
    <QueryState query={query}>{data=>data.custom.length?<ul className="space-y-2">{data.custom.map((p,i)=><li key={i}><code className="break-all">{p.pattern}</code> · {p.reason}</li>)}</ul>:<p>No saved patterns.</p>}</QueryState>
    <p className="text-muted">Pattern editing is temporarily read-only while its save route is integrated with scoped Security settings. Enforcement can be changed above without deleting these patterns.</p>
  </div></Card>;
}
