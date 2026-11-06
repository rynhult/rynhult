import {useState} from 'react';
import {Link} from 'react-router-dom';
import {ago,stamp} from '../../lib/format';
import {useSessions} from '../../lib/queries';
import {recentResults} from '../../lib/recentResults';
import {DocViewer,useKnowledge} from '../Knowledge';
import {Button,Card,CardHeader,Empty,QueryState} from '../ui';

/** Project-checkout notes use exactly the same source and viewer as Knowledge. */
export function RecentResults({projectId}:{projectId:number}) {
  const knowledge=useKnowledge(projectId);const sessions=useSessions();
  const [open,setOpen]=useState<string|null>(null);
  return <section aria-label="Recent results" className="min-w-0">
    <Card><CardHeader title="Recent results" subtitle="The latest notes saved in this project."
      action={<><Button size="sm" disabled={knowledge.isFetching} onClick={()=>{void knowledge.refetch();}}>Refresh results</Button>
        <Link className="inline-flex min-h-11 items-center rounded px-2 text-sm text-accent underline" to={`/projects/${projectId}#project-knowledge`}>View all</Link></>}/>
      {knowledge.isError&&knowledge.data!==undefined&&<p className="px-4 pt-3 text-sm text-wait" role="status">Results unavailable. These notes are from the last successful read.</p>}
      <QueryState query={knowledge} errorTitle="Results unavailable.">{value=>{
        const rows=recentResults(value,sessions.data??[],projectId);
        if(!rows.length)return <Empty>{knowledge.isError?'The last successful read contained no indexed results.':'Nothing saved here yet.'}</Empty>;
        return <ul className="divide-y divide-line">{rows.map(row=><li key={row.doc.path} className="min-w-0 space-y-1 px-4 py-3 text-sm">
          <p className="text-xs text-muted">{row.label} · {row.updated?<>Updated <time dateTime={row.updated} title={stamp(row.updated)}>{ago(row.updated)}</time></>:'Update time unavailable'}</p>
          <button type="button" className="block min-h-11 max-w-full break-words text-left font-medium text-accent underline [overflow-wrap:anywhere]" onClick={()=>setOpen(row.doc.path)}>{row.doc.path.split('/').pop()}</button>
          <p className="break-words text-xs text-muted [overflow-wrap:anywhere]">{sessions.isError?'Session attribution unavailable':sessions.data===undefined?'Checking session attribution…':row.session?
            <>Session: <Link className="inline-flex min-h-11 items-center rounded text-accent underline" to={`/sessions/${row.session.id}?tab=${row.kind==='handoffs'?'handoffs':'activity'}`}>{row.session.name}</Link></>:'Session not recorded'}</p>
        </li>)}</ul>;
      }}</QueryState>
    </Card>
    <DocViewer projectId={projectId} path={open} onClose={()=>setOpen(null)}/>
  </section>;
}
