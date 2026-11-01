import {useRef,useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {Link} from 'react-router-dom';
import {api,ApiError,post} from '../lib/api';
import {useOffline} from '../lib/queries';
import {SecurityVerification} from './SecurityVerification';
import {useConfirm} from './confirm';
import {announce,Button,Dialog,ErrorNote,Field,Input,QueryState,Select} from './ui';

type ACL=[number,number,number][];
interface Change {path:string;owner:number;group_before:number;group_after:number;mode_before:string;mode_add:string;access_before:ACL;access_after:ACL;default_before:ACL;default_after:ACL}
interface Preview {path:string;identity:number[];method:string;write:boolean;count:number;ancestors:string[];ancestor_changes?:{path:string;shared:boolean;access_before:ACL;access_after:ACL}[];changes:Change[];group?:{name:string;gid:number}|null}
interface Review {id:string;digest:string;scope:{machine:string;account:string};preview:Preview;undo:boolean;warning:string;active_sessions:string[];active_gates:string[]}
interface Operation {id:string;state:string;undo:boolean;approved:boolean;preview:Preview;result?:{conflicts?:number;remaining_access?:string;group_note?:string}}
const rights=(n:number)=>`${n&4?'r':'-'}${n&2?'w':'-'}${n&1?'x':'-'}`;
const acl=(entries:ACL)=>entries.map(([t,p,id])=>`${({1:'owner',2:'user',4:'owning group',8:'group',16:'mask',32:'other'} as Record<number,string>)[t]??t}${id<0?'':` ${id}`}: ${rights(p)}`).join('; ')||'Mode permissions (no extended ACL)';

export function FolderAccessSettings({machine,account}:{machine:string;account:string}) {
  const offline=useOffline(),client=useQueryClient(),confirm=useConfirm();
  const [open,setOpen]=useState(false),[path,setPath]=useState(''),[method,setMethod]=useState('named_acl'),[write,setWrite]=useState(false);
  const [review,setReview]=useState<Review|null>(null),[error,setError]=useState<unknown>(null),[busy,setBusy]=useState(false),[verify,setVerify]=useState(false);
  const pendingAction=useRef<()=>Promise<void>>(undefined),lock=useRef(false);
  const query=useQuery({queryKey:['folder-access',machine,account],queryFn:()=>api<{operations:Operation[]}>(`/api/security/access?${new URLSearchParams({machine,account})}`)});
  const refresh=async()=>{await Promise.all([query.refetch(),client.invalidateQueries({queryKey:['security-effective']}),client.invalidateQueries({queryKey:['security-scopes']})]);};
  const run=async(action:()=>Promise<void>)=>{
    if(lock.current)return;lock.current=true;setBusy(true);setError(null);
    try{await action();}catch(e){setError(e);void query.refetch();if(e instanceof ApiError&&e.status===403){pendingAction.current=action;setVerify(true);}}finally{lock.current=false;setBusy(false);}
  };
  const preview=()=>run(async()=>{
    const state=await api<{revision:number}>('/api/security/scopes');
    const result=await post<Review>('/api/security/access/preview',{machine,account,path,method,write,revision:state.revision});
    setReview(result);setOpen(false);
  });
  const undo=(id:string)=>run(async()=>{setReview(await post<Review>(`/api/security/access/${id}/undo-preview`,{}));});
  const apply=()=>run(async()=>{
    if(!review)return;
    const yes=await confirm({title:review.undo?'Undo this access grant?':'Grant filesystem access?',body:<div className="space-y-2"><p>{account} on {machine} · {review.preview.path}</p><p>{review.warning}</p><p>The account service may restart after its sessions stop. Repository contents are never deleted.</p></div>,confirm:review.undo?'Undo access grant':'Grant access',danger:true});
    if(!yes)return;
    const result=await post<{state:string}>(`/api/security/access/${review.id}/apply`,{digest:review.digest});
    setReview(null);await refresh();announce(`Access change: ${result.state}.`);
  });
  return <section aria-label="Filesystem access" className="space-y-3 border-t border-line pt-3">
    <h4 className="font-medium">Filesystem access</h4>
    <p>Linux only on qualified targets. macOS and Windows drives use existing access and local setup; machine isolation is unchanged.</p>
    <Button onClick={()=>{setError(null);setOpen(true);}} disabled={busy||offline}>Review filesystem access</Button>
    <ErrorNote error={error}/>
    <QueryState query={query}>{data=>data.operations.length?<ul className="space-y-3">{data.operations.map(o=><li key={o.id} className="space-y-2 rounded border border-line p-3 break-words">
      <p className="font-medium">{o.undo?'Undo: ':''}{o.preview.path}</p><p role="status">{o.state==='pending'?'Pending — target result unconfirmed; new work is blocked':o.state==='repair'||o.state==='partial'?'Needs repair — some access may remain; new work is blocked':o.state==='cancelled'?'Unstarted request cancelled; no OS changes':o.state==='undone'?'Recorded grant undone; other access may remain':'Applied — agent access checked'}</p>
      <p>{o.preview.method==='shared_group'?'Purpose-specific shared group':'Named-user ACL'} · {o.preview.write?'Read and write':'Read only'}</p>
      {o.result?.remaining_access&&<p>{o.result.remaining_access}</p>}{o.result?.group_note&&<p>{o.result.group_note}</p>}
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy||offline} onClick={()=>void run(async()=>{await post(`/api/security/access/${o.id}/refresh`,{});await refresh();})}>Check target result</Button>
      {!o.undo&&o.state==='pending'&&<Button size="sm" disabled={busy||offline} onClick={()=>void run(async()=>{await post(`/api/security/access/${o.id}/cancel`,{});await refresh();})}>Cancel unstarted request</Button>}
      {!o.undo&&!['undone','cancelled'].includes(o.state)&&<Button size="sm" disabled={busy||offline} onClick={()=>void undo(o.id)}>Review Undo</Button>}</div>
    </li>)}</ul>:<p>No access grants approved by your owner account. Existing OS permissions are unchanged.</p>}</QueryState>
    <Dialog open={open} title="Review filesystem access" dismissible={false} onClose={()=>{if(!busy)setOpen(false);}} footer={<><Button disabled={busy} onClick={()=>setOpen(false)}>Cancel</Button><Button variant="primary" disabled={busy||offline||!path} onClick={()=>void preview()}>Preview access changes</Button></>}>
      <div className="space-y-3"><p>{account} on {machine}. Checking and previewing do not change permissions.</p>
      <Field label="Existing repository folder"><Input autoFocus value={path} onChange={e=>setPath(e.target.value)} placeholder="/home/alex/src/demo-api" disabled={busy}/></Field>
      <Field label="Access method"><Select value={method} onChange={e=>setMethod(e.target.value)} disabled={busy}><option value="named_acl">Named-user ACL (recommended)</option><option value="shared_group">Purpose-specific shared group</option></Select></Field>
      <Field label="Agent access"><Select value={write?'write':'read'} onChange={e=>setWrite(e.target.value==='write')} disabled={busy}><option value="read">Read only</option><option value="write">Read and write — agent editing</option></Select></Field>
      {method==='shared_group'&&<p>Creates a dedicated group for the associated human and agent, changes reviewed group ownership, and sets directory inheritance. Other groups are not reused. New processes are required.</p>}<ErrorNote error={error}/></div>
    </Dialog>
    <Dialog open={!!review} title={review?.undo?'Review access Undo':'Review access grant'} dismissible={false} onClose={()=>{if(!busy)setReview(null);}} footer={<><Button disabled={busy} onClick={()=>setReview(null)}>Cancel</Button><Button variant="danger" disabled={busy||offline||!!review?.active_sessions.length||!!review?.active_gates.length} onClick={()=>void apply()}>{review?.undo?'Undo access grant':'Grant access'}</Button></>}>
      {review&&<div className="space-y-3 text-sm"><p className="break-words">{review.preview.path} · {account} on {machine}</p><p>{review.warning}</p><p>{review.preview.count} reviewed items · {review.preview.write?'Read and write':'Read only'}. Device / inode / mount: {review.preview.identity.join(' / ')}.</p>
      {!!review.active_sessions.length&&<p>Stop these sessions first: {review.active_sessions.map(id=><Link key={id} to={`/sessions/${id}`} className="mr-2 underline">{id.slice(0,8)}</Link>)}</p>}{!!review.active_gates.length&&<p>Cancel {review.active_gates.length} queued or running gates before continuing.</p>}
      <p>Ancestor traverse only (no listing, write or defaults): {review.preview.ancestors.join(', ')||'None required'}.</p>
      {!!review.preview.ancestor_changes?.length&&<details><summary className="cursor-pointer">Ancestor permission changes</summary><ul className="space-y-2 py-2">{review.preview.ancestor_changes.map(c=><li key={c.path} className="break-words">{c.path}: {acl(review.undo?c.access_after:c.access_before)} → {acl(review.undo?c.access_before:c.access_after)}. Defaults unchanged.{c.shared?' Shared traverse entry is retained while another grant depends on it.':''}</li>)}</ul></details>}
      {review.preview.group&&<p>Purpose group: {review.preview.group.name} (GID {review.preview.group.gid}). Empty groups are retained for administrator inode review after memberships are removed.</p>}
      <details><summary className="cursor-pointer">Exact permission changes for {review.preview.count} items{review.undo?' (reverse, only if unchanged)':''}</summary><ul className="space-y-3 py-3">{review.preview.changes.map(c=><li key={c.path} className="space-y-1 break-words"><strong>{c.path}</strong><p>Owner {c.owner} unchanged. Group {review.undo?c.group_after:c.group_before} → {review.undo?c.group_before:c.group_after}. {review.undo?`Restore mode ${c.mode_before} only if unchanged`:`Previous mode ${c.mode_before}; ${c.mode_add}` }.</p><p>Access: {acl(review.undo?c.access_after:c.access_before)} → {acl(review.undo?c.access_before:c.access_after)}.</p><p>Defaults: {(review.undo?c.default_after:c.default_before).length?acl(review.undo?c.default_after:c.default_before):'No default ACL'} → {(review.undo?c.default_before:c.default_after).length?acl(review.undo?c.default_before:c.default_after):'No default ACL'}.</p></li>)}</ul></details>
      <p>Review expires after five minutes. External permission changes may stop a grant midway. Undo never deletes working files.</p><ErrorNote error={error}/></div>}
    </Dialog>
    <SecurityVerification open={verify} onClose={()=>setVerify(false)} onVerified={()=>{setVerify(false);const action=pendingAction.current;pendingAction.current=undefined;if(action)void run(action);}}/>
  </section>;
}
