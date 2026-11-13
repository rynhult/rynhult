import {useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api,post} from '../lib/api';
import {Button,ErrorNote,QueryState,announce} from './ui';
import {useConfirm} from './confirm';
import {SecurityVerification} from './SecurityVerification';

type Evidence={id:string;state:string;recipe_id:string;recipe_digest:string;uid:number;runtime_id:string;cleanup:string;manifest:unknown[];steps:unknown[];next_offset:number|null;inspection?:{state?:string;reason?:string};inspection_digest?:string};
export default function InstallEvidence({operation}:{operation:string}) {
  const [offset,setOffset]=useState(0),[inspect,setInspect]=useState(false),[verify,setVerify]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null);
  const client=useQueryClient(),confirm=useConfirm();
  const query=useQuery({queryKey:['install-evidence',operation,offset,inspect],queryFn:()=>api<Evidence>(`/api/approvals/installs/${operation}?offset=${offset}&inspect=${inspect}`)});
  const reconcile=async(data:Evidence)=>{
    if(!await confirm({title:'Record this observed footprint?',body:'This records the current rehashed account prefix and clears the uncertain install block. It does not reinstall or delete files. Other processes in the Unix account can change files afterwards.',confirm:'Record footprint'}))return;
    setBusy(true);setError(null);
    try{await post(`/api/approvals/installs/${operation}/reconcile`,{expected_digest:data.inspection_digest});await client.invalidateQueries({queryKey:['install-evidence',operation]});await client.invalidateQueries({queryKey:['delegated-activity']});announce('Person reconciliation recorded.');}
    catch(e){setError(e);}finally{setBusy(false);}
  };
  return <div className="space-y-2"><QueryState query={query}>{data=><>
    <p>Recipe {data.recipe_id} · {data.recipe_digest}</p><p>UID {data.uid} · runtime {data.runtime_id} · {data.state} · cleanup {data.cleanup}</p>
    <p>Declared outputs</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(data.manifest,null,2)}</pre>
    <p>Publication and recovery evidence</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(data.steps,null,2)}</pre>
    {offset>0&&<Button size="sm" onClick={()=>setOffset(0)}>First evidence page</Button>}{data.next_offset!==null&&<Button size="sm" onClick={()=>setOffset(data.next_offset!)}>Next evidence page</Button>}
    <Button size="sm" disabled={busy} onClick={()=>{setInspect(true);void query.refetch();}}>Re-hash current prefix</Button>
    {data.inspection&&<p>Current observation: {data.inspection.state??data.inspection.reason}. This is not lasting integrity attestation.</p>}
    {data.state==='uncertain'&&data.inspection_digest&&data.inspection?.state&&<><Button size="sm" onClick={()=>setVerify(true)}>Verify identity for reconciliation</Button><Button size="sm" disabled={busy} onClick={()=>void reconcile(data)}>Record footprint and clear uncertainty</Button></>}
  </>}</QueryState><SecurityVerification open={verify} onClose={()=>setVerify(false)} onVerified={()=>setVerify(false)}/><ErrorNote error={error}/></div>;
}
