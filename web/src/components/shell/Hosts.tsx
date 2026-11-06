import { Surface as Card, ShellPage as Page, Workbar as PageHeader } from './PagePrimitives';
import {useQueries,useQuery} from '@tanstack/react-query';
import {Link,Navigate,useLocation,useSearchParams} from 'react-router-dom';
import {api} from '../../lib/api';
import {ago} from '../../lib/format';
import {useHealth,useInstance,useProjects,useRole,useSessions} from '../../lib/queries';
import {useNewShell} from '../../lib/uiShell';
import type {BoardView,HostView} from '../../lib/types';
import { Badge, Button, Dot, Empty, QueryState } from '../ui';
import {needsYou,sessionCount} from '../../lib/order';

type LocalReadings={cpu?:number|null;memory?:number|null;measuredAt?:number|null;receivedAt?:number|null;collectorAt?:number|null;disk?:number|null;snapshot:boolean;failed:boolean};
type Machine={name:string;label:string;local:boolean;connected:boolean|null;lastSeen?:string|null;at?:number|null;cpu?:number|null;memory?:number|null;disk?:number|null;stale?:boolean;readings?:LocalReadings;alerts?:BoardView['machines'][number]['alerts']};
const validTime=(at:number|null|undefined):at is number=>typeof at==='number'&&Number.isFinite(at)&&at>0&&at*1000<=8.64e15;
const old=(at:number|null|undefined)=>validTime(at)&&Date.now()/1000-at>300;
function Age({at}:{at:number|null|undefined}){
  if(!validTime(at))return <span>time unavailable</span>;
  const date=new Date(at*1000);return <time dateTime={date.toISOString()} title={date.toLocaleString()}>{ago(date.toISOString())}</time>;
}
/** Main health needs /host: board's local light mixes live metrics and collector disks.
 * Members use this same already-filtered /host endpoint and never request board. */
function useHostSummaries() {
  const {owner,known}=useRole();const instance=useInstance();const health=useHealth();
  const local=useQuery({queryKey:['host'],queryFn:()=>api<HostView>('/api/host'),enabled:known,refetchInterval:10000});
  const board=useQuery({queryKey:['board'],queryFn:()=>api<BoardView>('/api/board'),enabled:known&&owner,refetchInterval:10000});
  const query=owner?board:local;
  const machines:Machine[]|undefined=owner?board.data?.machines.map(m=>({name:m.name,label:m.label,local:m.local,connected:m.connected,lastSeen:m.last_seen,
    at:m.light?.collected_at,cpu:m.light?.cpu_percent,memory:m.light?.memory_percent,disk:m.light?.disk?.percent,stale:m.stale,alerts:m.alerts})):
    local.data?[{name:'local',label:instance.host,local:true,connected:health.data?.broker??null}]:undefined;
  const snapshot=local.data?.snapshot;
  const disk=snapshot?.disks?.reduce<number|null>((fullest,d)=>fullest==null?d.percent:Math.max(fullest,d.percent),null);
  return {...query,error:query.error??local.error,isFetching:query.isFetching||local.isFetching,
    refetch:()=>Promise.all([local.refetch(),...(owner?[board.refetch()]:[])]),
    data:machines?.map(m=>({...m,connected:query.isError||m.local&&health.isError?null:m.local&&health.data?.broker===false?false:m.connected,
      at:m.local?local.dataUpdatedAt/1000||null:m.at,stale:!m.local&&(!!m.stale||old(m.at)),
      readings:m.local?{cpu:local.data?.live?.cpu_percent,memory:local.data?.live?.memory?.percent,measuredAt:local.data?.live?.measured_at,
        receivedAt:local.dataUpdatedAt/1000||null,collectorAt:snapshot?.collected_at,disk,snapshot:!!snapshot,failed:local.isError}:undefined,
    }))};
}
function Freshness({machine:m}:{machine:Machine}) {
  return <span>{m.connected!==true?<>Last seen {m.lastSeen?ago(m.lastSeen):validTime(m.at)?<Age at={m.at}/>:'not reported'}</>:
    validTime(m.at)?<>{m.stale?'Stale · ':''}{m.local?'Response received':'Node summary observed'} <Age at={m.at}/></>:'Observation time unavailable'}</span>;
}
function HealthSummary({machine:m}:{machine:Machine}) {
  if(m.connected!==true)return <span>Health: Unavailable{m.at?' · last observation retained':''}</span>;
  const percent=(n:number|null|undefined)=>n==null?'unavailable':`${Math.round(n)}%`;
  const readings=m.readings;
  const alerts=[...new Set([...(m.alerts??[]).map(a=>a.title),...(readings&&old(readings.collectorAt)?['Host health readings are out of date']:[])])];
  return <span>
    {readings?<>
      {readings.failed&&<span className="block text-wait">Local health refresh failed; retained readings follow.</span>}
      <span role="group" aria-label="Live readings" className="block">{readings.failed?'Last live psutil reading':'Live psutil'} · CPU {percent(readings.cpu)} · Memory {percent(readings.memory)} · {validTime(readings.measuredAt)?'Measured':'Received'} <Age at={validTime(readings.measuredAt)?readings.measuredAt:readings.receivedAt}/>{old(readings.measuredAt??readings.receivedAt)?' · Stale':''}</span>
      <span role="group" aria-label="Collector readings" className="block">Collector snapshot · Disk {percent(readings.disk)}{readings.disk!=null?' (fullest)':''} · {old(readings.collectorAt)?'Stale · ':''}{readings.snapshot?<>Observed <Age at={readings.collectorAt}/></>:'unavailable'}</span>
    </>:<span className="block">Node summary · {m.stale?'Last reading · ':''}CPU {percent(m.cpu)} · Memory {percent(m.memory)}{m.disk!=null?` · Disk ${percent(m.disk)}`:''} · Observed <Age at={m.at}/></span>}
    {alerts.map(title=><span className="block text-wait" key={title}>{title}</span>)}
  </span>;
}
function ActiveWork({machine,compact=false}:{machine:Machine;compact?:boolean}) {
  const sessions=useSessions();const rows=sessions.data?.filter(s=>(s.host??'local')===machine.name&&s.desired_state!=='archived');
  const received=sessions.dataUpdatedAt/1000;
  if(machine.connected!==true)return <span>Active work: unavailable{rows?<>{` · ${rows.length} saved session${rows.length===1?'':'s'}`} · Last received <Age at={received}/></>:null}</span>;
  if(!rows)return <span>Active work: {sessions.isError?'Unavailable':'checking…'}</span>;
  if(sessions.isError)return <span>Active work: unavailable · Last received <Age at={received}/> · Saved counts: Working {sessionCount(rows,'working')} · Needs you {needsYou(rows).length} · {rows.length} visible sessions</span>;
  return <span>{compact?'Work':'Active work'}: Working {sessionCount(rows,'working')} · Needs you {needsYou(rows).length} · {rows.length} visible sessions · Received <Age at={received}/></span>;
}
export function HostsSidebar({close}:{close:()=>void}) {
  const query=useHostSummaries();const route=useLocation();const machine=new URLSearchParams(route.search).get('machine');
  const current=(m:{local?:boolean;name:string})=>route.pathname==='/host'&&(m.local?!machine:machine===m.name);
  return <QueryState query={query}>{machines=>machines.map(m=><Link className="shell-link shell-host-link" aria-current={current(m)?'page':undefined} key={m.name} to={m.local?'/host':`/host?machine=${encodeURIComponent(m.name)}`} onClick={close}>
      <span aria-hidden="true" title={m.connected===true?'Connected':m.connected===false?'Offline':'Unavailable'}><Dot tone={m.connected===true?'ok':m.connected===false?'bad':'neutral'}/></span>
      <span className="font-medium">{m.label}</span>
    </Link>)}</QueryState>;

}
export default function Hosts() {
  const shell=useNewShell();
  return shell?<HostsView/>:<Navigate to="/host" replace/>;
}
function HostsView() {
  const query=useHostSummaries();const {owner}=useRole();const [params]=useSearchParams();
  const selected=owner?params.get('machine'):null;
  return <Page wide><PageHeader title="Hosts" description="Connected machines and their working locations. Host health stays scoped to each machine." actions={<Button disabled={query.isFetching} onClick={()=>{void query.refetch();}}>Refresh hosts</Button>}/>
    <QueryState query={query}>{machines=><>
      {selected&&<p className="mb-4"><Link className="text-accent underline" to="/hosts">All hosts</Link></p>}
      {selected&&!machines.some(m=>m.name===selected)&&<Empty>This host is not available in your current host list.</Empty>}
      <div className="grid min-w-0 gap-6 xl:grid-cols-2">{machines.filter(m=>!selected||m.name===selected).map(m=><section key={m.name} aria-label={m.label} className="min-w-0">
        <Card className="h-full space-y-4 p-4"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="break-words font-semibold">{m.label}</h2><Badge tone={m.connected===true?'neutral':'wait'}>{m.connected===true?'Connected':m.connected===false?'Offline':'Unavailable'}</Badge></div>
          <p className="text-sm text-muted">{m.local?'Main system':m.name} · <Freshness machine={m}/></p>
          <p className="text-sm"><HealthSummary machine={m}/></p><div className="text-sm"><ActiveWork machine={m}/></div>
          <WorkingLocations machine={m.name}/>
          <Link className="inline-flex min-h-11 items-center text-sm text-accent underline" to={m.local?'/host':`/host?machine=${encodeURIComponent(m.name)}`}>Open host</Link>
        </Card>
      </section>)}</div>
    </>}</QueryState>
    <p className="mt-6 text-sm text-muted">Offline observations remain dated; files and sessions are never moved or restarted automatically. Open host shows the existing health, account and service details your role can inspect.</p>
  </Page>;
}
function WorkingLocations({machine}:{machine:string}) {
  const projects=useProjects();const instance=useInstance();
  return <section aria-label="Working locations" className="space-y-2 border-t border-line pt-3"><h3 className="font-medium">Working locations</h3>
    <QueryState query={projects}>{rows=>{const local=rows.filter(p=>(p.host??'local')===machine);return local.length?<ul className="space-y-2">{local.map(p=><li key={p.id} className="break-all text-sm"><Link className="text-accent underline" to={`/projects/${p.id}`}>{p.name}</Link><p className="text-muted">{p.account??'Account not reported'} · {p.workspace_path}{p.archived?' · Archived':''}</p></li>)}</ul>:<p className="text-sm text-muted">No personal project locations recorded here.</p>;}}</QueryState>
    {instance.features?.groups!==false&&<GroupLocations machine={machine}/>}
  </section>;
}
type GroupProject={id:number;name:string;archived?:boolean};
type Placement={id:number;host:string;account:string;path:string;sync_status:string;trust:string};
function GroupLocations({machine}:{machine:string}) {
  const {owner}=useRole();
  const groups=useQuery({queryKey:['groups'],queryFn:()=>api<{id:string;role:string}[]>('/api/groups'),refetchInterval:10000});
  const eligible=(groups.data??[]).filter(g=>owner||g.role!=='viewer');
  const lists=useQueries({queries:eligible.map(g=>({queryKey:['group-projects',g.id],queryFn:()=>api<GroupProject[]>(`/api/groups/${g.id}/projects`),refetchInterval:10000}))});
  return <QueryState query={groups}>{()=> <>{lists.some(q=>q.isPending)&&<p className="text-sm text-muted">Checking group projects…</p>}{lists.some(q=>q.isError)&&<p className="text-sm text-wait">Some group locations are unavailable.</p>}
    {eligible.flatMap((g,i)=>(lists[i].data??[]).map(p=><GroupPlacements key={`${g.id}:${p.id}`} machine={machine} group={g.id} project={p}/>))}
  </>}</QueryState>;
}
function GroupPlacements({machine,group,project}:{machine:string;group:string;project:GroupProject}) {
  const query=useQuery({queryKey:['group-placements',group,project.id],queryFn:()=>api<Placement[]>(`/api/groups/${group}/projects/${project.id}/placements`),refetchInterval:10000});
  return <QueryState query={query}>{rows=><ul className="space-y-2">{rows.filter(p=>p.host===machine).map(p=><li key={p.id} className="break-all text-sm"><Link className="text-accent underline" to={`/groups?group=${encodeURIComponent(group)}&tab=projects`}>{project.name}</Link><p className="text-muted">{p.account} · {p.path} · {p.sync_status} · {p.trust}{project.archived?' · Archived':''}</p></li>)}</ul>}</QueryState>;
}
