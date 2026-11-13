import {useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api,post} from '../lib/api';
import {useOffline,useRole,useSessions} from '../lib/queries';
import {Button,ErrorNote,Field,Input,QueryState,Select,announce} from './ui';
import {SecurityVerification} from './SecurityVerification';

type Image={provider:string;model:string;uid:number;runtime_id:string};
type Recipe={digest:string;recipe:{id:string;version:string;registry:string;bins:Record<string,string>;manifest:{path:string}[];runtime_id:string;limits:Record<string,number>}};
type Configuration={enabled:boolean;available:boolean;revision:number;security_revision:number;images:Image[];recipes:Recipe[];install_identity?:{uid:number;runtime_id:string};scope?:{project_id:number;team_id:string;account:string;machine:string;machine_id:string;issuer:string}};

export default function DelegatedSetup({projectId}:{projectId?:number}) {
  const role=useRole(),offline=useOffline(),sessions=useSessions(),client=useQueryClient();
  const [team,setTeam]=useState(''),[enabled,setEnabled]=useState<boolean|null>(null),[ports,setPorts]=useState(false),[image,setImage]=useState(false),[recipe,setRecipe]=useState('');
  const [pool,setPool]=useState('dev'),[count,setCount]=useState(1),[duration,setDuration]=useState('team'),[expiry,setExpiry]=useState(''),[requestDigest,setRequestDigest]=useState('');
  const [verify,setVerify]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null),[review,setReview]=useState(false);
  const query=useQuery({queryKey:['delegation-configuration',role.username,team],queryFn:()=>api<Configuration>(`/api/approvals/configuration${team?`?team_id=${encodeURIComponent(team)}`:''}`)});
  const teams=sessions.data?.filter(s=>s.team_id===s.id&&!s.parent_session_id&&s.owner===role.username&&s.desired_state==='running'&&(!projectId||s.project_id===projectId)&&(!s.host||s.host==='local')&&!s.container)??[];
  const edit=()=>setReview(false);
  const save=async(data:Configuration)=>{
    setBusy(true);setError(null);
    const scope=data.scope,consents=[];
    const selected=data.recipes.find(r=>r.digest===recipe);
    if(((ports||image||!!recipe)&&!scope)||(image&&!data.images[0])||(!!recipe&&(!selected||!data.install_identity))){
      setError(new Error('The selected scope or qualification changed. Review your draft before saving.'));setBusy(false);setReview(false);return;
    }
    const base=scope?{project_id:scope.project_id,team_id:scope.team_id,duration,expires_at:expiry?new Date(expiry).toISOString():null,request_digest:duration==='once'?requestDigest:null}:null;
    if(base&&ports)consents.push({...base,action:'port.reserve',constraints:{range:pool,max_reservations:count}});
    if(base&&image&&data.images[0])consents.push({...base,action:'image.check',constraints:data.images[0]});
    if(base&&selected&&data.install_identity)consents.push({...base,action:'tool.install',constraints:{recipe_id:selected.recipe.id,recipe_digest:selected.digest,uid:data.install_identity.uid,runtime_id:data.install_identity.runtime_id,max_installs:5,limits:selected.recipe.limits}});
    try{
      await post('/api/approvals/configuration',{enabled:enabled??data.enabled,revision:data.revision,security_revision:data.security_revision,consents});
      setPorts(false);setImage(false);setRecipe('');setEnabled(null);setReview(false);
      await client.invalidateQueries({queryKey:['delegation-configuration']});await client.invalidateQueries({queryKey:['delegated-consents']});
      announce('Delegation availability and selected permissions saved.');
    }catch(e){setError(e);await query.refetch();setReview(false);}finally{setBusy(false);}
  };
  return <section className="space-y-3" aria-label="Delegation enablement and consent"><h3 className="font-medium">Delegated approvals</h3>
    <QueryState query={query}>{data=><>
      <p>Saved availability: {data.enabled?'On':'Off'}. Enablement alone grants nothing. Teams availability: {data.available?'On':'Off'}. Current authority still applies.</p>
      <label className="flex items-center gap-2"><input type="checkbox" checked={enabled??data.enabled} disabled={role.role!=='owner'||busy||offline} onChange={e=>{setEnabled(e.target.checked);edit();}}/>Enable delegated approvals</label>
      <Field label="Build team"><Select value={team} onChange={e=>{setTeam(e.target.value);setPorts(false);setImage(false);setRecipe('');edit();}}><option value="">Choose your active local team</option>{teams.map(s=><option value={s.id} key={s.id}>{s.name} · {s.project} · {s.run_as}</option>)}</Select></Field>
      {team&&!data.scope&&<p>This team has no eligible whole-account consent scope. Refresh or choose an active Build team.</p>}
      {data.scope&&<>
        <p>Issuer {data.scope.issuer} · {data.scope.machine} / {data.scope.account} · team {data.scope.team_id}</p>
        <p>Any process in this Unix account can use this consent. The lead’s approval does not isolate helpers.</p>
        <fieldset className="space-y-2"><legend className="font-medium">Ports</legend>
          <label className="flex items-center gap-2"><input type="checkbox" checked={ports} onChange={e=>{setPorts(e.target.checked);edit();}}/>Allow bounded port reservations</label>
          <p>Local registry only; grants no publication, listener or firewall authority.</p>
          {ports&&<div className="flex flex-wrap gap-3"><Field label="Port pool"><Select value={pool} onChange={e=>{setPool(e.target.value);edit();}}><option value="dev">Development</option><option value="prod">Production</option></Select></Field><Field label="Aggregate reservation count"><Input type="number" min={1} max={10} value={count} onChange={e=>{setCount(Number(e.target.value));edit();}}/></Field></div>}
        </fieldset>
        <fieldset className="space-y-2"><legend className="font-medium">Image checks</legend>
          <label className="flex items-center gap-2"><input type="checkbox" checked={image} disabled={!data.images.length} onChange={e=>{setImage(e.target.checked);edit();}}/>Allow this team’s exact model check</label>
          {data.images[0]?<p>{data.images[0].provider} / {data.images[0].model} · UID {data.images[0].uid} · runtime {data.images[0].runtime_id}. Two checks per model and five per account per day. Read-only synthetic probe; no private references are sent.</p>:<p>Model and account identity are unqualified or unavailable.</p>}
        </fieldset>
        <fieldset className="space-y-2"><legend className="font-medium">Account installs</legend>
          {!data.recipes.length?<p>Installs are unqualified. No reviewed recipe can be consented to until owner qualification on disposable hosts.</p>:<Field label="Reviewed recipe"><Select value={recipe} onChange={e=>{setRecipe(e.target.value);edit();}}><option value="">No install permission</option>{data.recipes.map(r=><option key={r.digest} value={r.digest}>{r.recipe.id} {r.recipe.version}</option>)}</Select></Field>}
          {data.recipes.filter(r=>r.digest===recipe).map(r=><div key={r.digest} className="break-words"><p>{r.recipe.id} {r.recipe.version} · {r.recipe.registry} · digest {r.digest}</p><p>UID {data.install_identity?.uid} · runtime {r.recipe.runtime_id}.</p><p>Limits: {Object.entries(r.recipe.limits).map(([key,value])=>`${key}: ${value}`).join(', ')}.</p><p>Commands: {Object.keys(r.recipe.bins).join(', ')}. Outputs: {r.recipe.manifest.map(e=>e.path).join(', ')}.</p><p>Any process in this Unix account may install these exact recipe digests, up to 5 per day, affecting every project on the account. The lead’s approval does not isolate helpers. Revoking does not uninstall. Scripts are disabled; no package code runs.</p></div>)}
        </fieldset>
        <Field label="Permission duration"><Select value={duration} onChange={e=>{setDuration(e.target.value);edit();}}><option value="once">Once</option><option value="team">This team</option><option value="project">Remember for this project</option></Select></Field>
        {duration==='once'&&<Field label="Exact reviewed request digest"><Input value={requestDigest} onChange={e=>{setRequestDigest(e.target.value);edit();}}/></Field>}
        <Field label="Optional expiry"><Input type="datetime-local" value={expiry} onChange={e=>{setExpiry(e.target.value);edit();}}/></Field>
      </>}
      <Button size="sm" onClick={()=>setVerify(true)}>Verify before saving</Button>
      <SecurityVerification open={verify} onClose={()=>setVerify(false)} onVerified={()=>{setVerify(false);announce('Identity verified. Review and save your delegation draft.');}}/>
      {review&&<div role="status" className="space-y-2"><p>Proposed change: delegation {enabled??data.enabled?'On':'Off'}; {ports?'port reservations':'no new port consent'}; {image?'exact model checks':'no new image consent'}; {recipe?'selected recipe':'no new install consent'}.</p><p>Duration: {duration}. {expiry?`Expires ${expiry}.`:'Team permissions end with the team; project permissions remain until expiry or revoke.'} All selected changes save together.</p></div>}
      <Button disabled={busy||offline||!!query.error||((ports||image||!!recipe)&&!data.scope)||(image&&!data.images[0])||(!!recipe&&(!data.recipes.some(r=>r.digest===recipe)||!data.install_identity))||((ports||image||!!recipe)&&!(enabled??data.enabled))||(duration==='once'&&(ports||image||!!recipe)&&! /^[0-9a-f]{64}$/.test(requestDigest))} onClick={()=>review?void save(data):setReview(true)}>{busy?'Saving…':review?'Save delegation and permissions':'Review delegation changes'}</Button>
    </>}</QueryState><ErrorNote error={error}/>
  </section>;
}
