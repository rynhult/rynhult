import {lazy,Suspense,useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api,post} from '../lib/api';
import {useOffline,useRole} from '../lib/queries';
import {SettingsCard,SettingsCardHeader} from './shell/SettingsSurfaces';
import {Button,ErrorNote,QueryState,announce} from './ui';
import {useConfirm} from './confirm';

const DelegatedSetup=lazy(()=>import('./DelegatedSetup'));

type Consent={parent_id?:string|null;remaining?:number|null;remaining_allowance?:number|null;id:string;revision:number;issuer:string;action:string;team_id:string;project_id:number|null;state:string;effective_state:string;reason:string;constraints:Record<string,unknown>;expires_at:string|null;last_used_at?:string|null;duration?:string;authority?:{placement?:{machine:string;account:string;machine_id:string}}};
type Page={consents:Consent[];next_cursor:string|null};
const reasons:Record<string,string>={ready_for_bound_lead_admission:'Eligible for a lead request; execution checks still apply.',execution_still_requires_qualification:'Execution is not qualified for this action.'};

export function DelegatedConsents({projectId}:{projectId?:number}) {
  const role=useRole(),offline=useOffline(),client=useQueryClient(),confirm=useConfirm();
  const [cursor,setCursor]=useState<string|null>(null),[history,setHistory]=useState(false),[busy,setBusy]=useState<string|null>(null),[error,setError]=useState<unknown>(null);
  const query=useQuery({queryKey:['delegated-consents',role.username,projectId,history,cursor],queryFn:()=>api<Page>(`/api/approvals/consents?state=${history?'revoked':'active'}${projectId?`&project_id=${projectId}`:''}${cursor?`&cursor=${cursor}`:''}`)});
  const attempts=useQuery({queryKey:['delegated-refusals',role.username,projectId],queryFn:()=>api<{refusals:{id:number;at:string;lead_id:string;action:string;reason:string}[]}>(`/api/approvals/activity${projectId?`?project_id=${projectId}`:''}`),refetchInterval:15000});
  const revoke=async(row:Consent)=>{
    if(!await confirm({title:'Revoke saved permission?',body:'New delegated operations will be denied. Work already dispatched may finish. Revocation keeps the consent and audit history.',confirm:'Revoke permission',danger:true}))return;
    setBusy(row.id);setError(null);
    try{await post(`/api/approvals/consents/${row.id}/revoke`,{revision:row.revision});await client.invalidateQueries({queryKey:['delegated-consents']});announce('Permission revoked. Already dispatched work may finish.');}
    catch(e){setError(e);await query.refetch();}finally{setBusy(null);}
  };
  return <SettingsCard><SettingsCardHeader title="Saved delegated permissions" subtitle="Person-issued consent for exact team and account scopes."/><div className="space-y-3 p-4 text-sm">
    <Suspense fallback={<p>Loading delegation controls…</p>}><DelegatedSetup projectId={projectId}/></Suspense>
    <p>Delegation availability does not grant permission. You can revoke saved consent even when delegation is disabled.</p>
    <Button size="sm" onClick={()=>{setHistory(!history);setCursor(null);}}>{history?'Show active permissions':'Show revoked history'}</Button>
    <QueryState query={query}>{data=><>
      {data.consents.length?<ul className="space-y-4">{data.consents.map(row=><li key={row.id} className="space-y-2 break-words">
        <p className="font-medium">{row.action} · project {row.project_id??'removed'} · {row.issuer}</p>
        <p>Team {row.team_id} · {row.state} · {row.effective_state} · revision {row.revision}</p>
        {row.authority?.placement&&<p>{row.authority.placement.machine} / {row.authority.placement.account} · machine identity {row.authority.placement.machine_id}</p>}
        {row.parent_id&&<p>Parent permission {row.parent_id}</p>}
        {row.remaining_allowance!==null&&row.remaining_allowance!==undefined&&<p>Remaining allowance: {row.remaining_allowance}. Other execution checks still apply.</p>}
        <p>{row.duration??'Scoped permission'} · {row.last_used_at?`last used ${new Date(row.last_used_at).toLocaleString()}`:'Not used'}</p>
        <p>{reasons[row.reason]??row.reason}{row.expires_at?` · expires ${new Date(row.expires_at).toLocaleString()}`:''}</p>
        <dl>{Object.entries(row.constraints).map(([name,value])=><div key={name}><dt className="inline font-medium">{name}: </dt><dd className="inline [overflow-wrap:anywhere]">{typeof value==='object'?JSON.stringify(value):String(value)}</dd></div>)}</dl>
        {row.state==='active'&&<Button size="sm" disabled={offline||!!busy||!!query.error} onClick={()=>void revoke(row)}>{busy===row.id?'Revoking…':'Revoke permission'}</Button>}
      </li>)}</ul>:<p>No {history?'revoked':'active'} saved permissions in this scope.</p>}
      <div className="flex gap-2">{cursor&&<Button size="sm" onClick={()=>setCursor(null)}>First page</Button>}{data.next_cursor&&<Button size="sm" onClick={()=>setCursor(data.next_cursor)}>Next page</Button>}</div>
    </>}</QueryState>
    <h3 className="font-medium">Refused delegated attempts</h3>
    <p className="text-muted">Latest 50 · repeated refusals are recorded at most once per lead, action and reason each minute.</p>
    <QueryState query={attempts}>{data=>data.refusals?.length?<ul className="space-y-2">{data.refusals.map(row=><li key={row.id}>{row.action} · {row.reason} · lead {row.lead_id} · {new Date(row.at).toLocaleString()}</li>)}</ul>:<p>No recorded refusals in this scope.</p>}</QueryState>
    <ErrorNote error={error}/>
  </div></SettingsCard>;
}
