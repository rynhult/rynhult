import type {KnowledgeView} from '../components/Knowledge';
import type {SessionSummary} from './types';

type VisibleSession=Pick<SessionSummary,'id'|'name'|'project_id'>;
const SOURCES=[['handoffs','Handoff'],['council','Council report'],['research','Research findings']] as const;

/** A bounded projection of the existing Knowledge index, not a completion feed.
 * Only the caller's authorized session list can supply attribution. */
export function recentResults(knowledge:KnowledgeView,sessions:VisibleSession[],projectId:number) {
  const visible=sessions.filter(s=>s.project_id===projectId);
  return SOURCES.flatMap(([kind,label])=>(knowledge.docs[kind]??[]).map(doc=>({doc,kind,label})))
    .map(row=>{
      const millis=row.doc.modified*1000;
      const updated=Number.isFinite(millis)&&millis>0&&millis<=8.64e15?new Date(millis).toISOString():null;
      const name=row.doc.path.split('/').pop()??'';
      const matches=visible.filter(s=>{
        const id=s.id.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
        return new RegExp(`(^|[^a-zA-Z0-9])${id}($|[^a-zA-Z0-9])`).test(name);
      });
      return {...row,updated,session:matches.length===1?matches[0]:undefined};
    })
    .sort((a,b)=>(b.updated?Date.parse(b.updated):0)-(a.updated?Date.parse(a.updated):0)||a.doc.path.localeCompare(b.doc.path))
    .slice(0,5);
}
