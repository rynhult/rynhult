import {lazy,Suspense,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api} from '../lib/api';
import {useRole} from '../lib/queries';
import {QueryState} from './ui';

const InstallEvidence=lazy(()=>import('./InstallEvidence'));
function Evidence({operation}:{operation:string}){const[open,setOpen]=useState(false);return <details onToggle={e=>setOpen(e.currentTarget.open)}><summary className="cursor-pointer py-2">Inspect recipe and footprint evidence</summary>{open&&<Suspense fallback={<p>Loading install evidence…</p>}><InstallEvidence operation={operation}/></Suspense>}</details>;}

type Refusal={id:number;action:string;reason:string;lead_id:string};
type Operation={id:string;action:string;state:string;decision:{issuer:string;lead_id:string};result:Record<string,unknown>|null;created_at:string};
export function DelegatedDigest() {
  const role=useRole();
  const query=useQuery({queryKey:['delegated-activity',role.username],queryFn:()=>api<{operations:Operation[];refusals?:Refusal[]}>('/api/approvals/activity'),refetchInterval:15000});
  return <section aria-label="Delegated activity" className="rounded border border-line p-4 text-sm">
    <h2 className="font-semibold">Team activity</h2>
    <p className="text-muted">Delegated decisions and outcomes · latest 50 operations</p>
    <QueryState query={query}>{data=><>
      {Object.entries(data.operations.filter(row=>row.result?.state==='already_installed').reduce<Record<string,number>>((groups,row)=>{const key=String(row.result?.recipe_digest);groups[key]=(groups[key]??0)+1;return groups;},{})).map(([recipe,count])=><p key={recipe}>{count} matching already-installed admissions · recipe {recipe}</p>)}
      {!!data.refusals?.length&&<details><summary className="cursor-pointer py-2">Refused delegated attempts</summary><ul>{Object.entries(data.refusals.reduce<Record<string,number>>((groups,row)=>{const key=row.action+' · '+row.reason;groups[key]=(groups[key]??0)+1;return groups;},{})).map(([reason,count])=><li key={reason}>{count} · {reason}</li>)}</ul></details>}
      {data.operations.length?<details><summary className="cursor-pointer py-2">{data.operations.length} delegated operations</summary>
      <ul className="space-y-3">{data.operations.map(row=><li key={row.id} className="[overflow-wrap:anywhere]">
        <p>{row.action} · {row.state} · {new Date(row.created_at).toLocaleString()}</p>
        <p>Allowed by saved permission from {row.decision.issuer} · lead {row.decision.lead_id}</p>
        <p>Operation {row.id}</p>{row.action==='tool.install'&&<Evidence operation={row.id}/>}
        {row.result&&<dl>{Object.entries(row.result).filter(([key])=>['port','range','image_input','reason','state','recipe_digest','footprint_digest','cleanup'].includes(key)).map(([key,value])=><div key={key}><dt className="inline">{key}: </dt><dd className="inline">{String(value)}</dd></div>)}</dl>}
      </li>)}</ul></details>:<p className="py-2 text-muted">No delegated activity yet.</p>}</>}</QueryState>
  </section>;
}
