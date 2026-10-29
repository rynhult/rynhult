/** Slice 3 exports: the live-tree and Settings owners mount these components. */
import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post, isNotFound } from '../lib/api';
import { useSingleMutation } from '../lib/mutations';
import { Button, Card, ErrorNote, Field, Input, QueryState, Select, Textarea } from './ui';

interface Candidate {
  agent: string; profile: string | null; model: string | null; machine: string; permission: string;
  score: number; reasons: string[]; eligible: boolean; rejections: string[];
  quota?: {status: string; used_percent?: number; headroom_percent?: number};
  est_cost: {value: number; unit: string; source: string};
}
interface Selection {
  id: string; state: string; chosen: number | null; reason: string; need: {task_type: string; role?: string; risk?: string; author_session?: string};
  assignment_id?: string; catalog_revision?: string; profile_digest?: string; classification_reason?: string;
  classification?: {task_type: string; role: string};
  candidates: Candidate[]; override_by?: string; override_reason?: string;
  budget: {providers: Record<string,{remaining: number | null}>; policy_hash?: string};
}
interface Choice { agent: string; models: string[] }
interface Rule { profile: string; task_type: string; choices: Choice[] }
interface QuotaPolicy { enabled: boolean; max_used_percent: number; max_age_minutes: number }
interface Policy { version: number; rules: Rule[]; quota?: QuotaPolicy }
const tasks=['implementation','design','security_review','docs','small_fix','review','discovery'];
const words=(value: string)=>value.replaceAll('_',' ');

function SelectionRow({row,teamId}: {row: Selection;teamId: string}) {
  const client=useQueryClient();
  const [choice,setChoice]=useState(row.chosen??0);
  const [reason,setReason]=useState('');
  useEffect(()=>setChoice(row.chosen??0),[row.chosen]);
  const change=useSingleMutation({mutationFn:()=>post(`/api/teams/${teamId}/selections/${row.id}/override`,{choice,reason}),
    onSuccess:()=>{setReason('');client.invalidateQueries({queryKey:['selections',teamId]});}});
  const chosen=row.chosen===null?null:row.candidates[row.chosen];
  const pending=['proposed','overridden'].includes(row.state);
  return <article className="space-y-3 border-t border-line py-3 first:border-0">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">{words(row.need.task_type)}</h3>
      <span className="text-xs text-muted">{words(row.state)}</span></div>
    {chosen&&<p className="break-words text-sm">{chosen.agent} · {chosen.profile||'No profile'} · {chosen.model} · {chosen.machine} · {chosen.permission}</p>}
    <p className="text-sm">{row.reason}</p>
    <p className="text-xs text-muted">Lead classification: {words(row.classification?.task_type || row.need.task_type)} · {row.classification?.role || row.need.role || 'legacy classification'}. {row.classification_reason}</p>
    <p className="text-xs text-muted">Risk: {row.need.risk || 'low'} (recorded only; does not affect ranking).</p>
    {row.budget.policy_hash && <p className="break-all text-xs text-muted">Policy hash: {row.budget.policy_hash}</p>}
    {row.catalog_revision && <dl className="grid min-w-0 gap-1 text-xs text-muted">
      <div><dt className="inline">Catalog revision: </dt><dd className="inline break-all">{row.catalog_revision}</dd></div>
      <div><dt className="inline">Profile digest: </dt><dd className="inline break-all">{row.profile_digest || 'No eligible profile'}</dd></div>
    </dl>}
    {row.assignment_id && <p className="break-all text-xs text-muted">Assignment: {row.assignment_id}</p>}
    {row.need.author_session&&<p className="break-all text-xs text-muted">Author session: {row.need.author_session}. Review starts a distinct new session.</p>}
    {chosen&&<><ul className="list-disc space-y-1 pl-5 text-xs text-muted">{chosen.reasons.slice(0,3).map((reason,i)=><li key={i}>{reason}</li>)}</ul>
      <p className="text-xs text-muted">Estimated {chosen.est_cost.value}% plan usage · {chosen.est_cost.source}. Remaining: {row.budget.providers[chosen.agent]?.remaining==null?'no configured cap':`${row.budget.providers[chosen.agent].remaining}%`}. This is an estimate, not a bill.</p></>}
    {chosen?.quota&&<p className="text-xs text-muted">{chosen.quota.status==='fresh'
      ? `Fresh provider quota: ${chosen.quota.used_percent?.toFixed(1)}% used · ${chosen.quota.headroom_percent?.toFixed(1)}% headroom.`
      : 'Provider quota is stale or unknown. Existing selection rules apply.'}</p>}
    {row.override_by&&<p className="text-xs text-muted">Override recorded by {row.override_by}</p>}
    {row.override_reason&&<p className="whitespace-pre-wrap break-words text-sm" aria-live="polite">{row.override_reason}</p>}
    <details><summary className="cursor-pointer text-xs">Candidates and filters</summary><ul className="mt-2 space-y-2 text-xs">
      {row.candidates.map((candidate,i)=><li key={i} className="break-words">{candidate.agent} · {candidate.model||'No known model'} · score {candidate.score}
        {!candidate.eligible&&<span className="text-wait"> · Unavailable: {candidate.rejections.join('; ')}</span>}</li>)}</ul></details>
    {pending&&<form aria-label={`Override selection ${row.id}`} className="space-y-3" onSubmit={event=>{event.preventDefault();if(reason.trim())change.mutate();}}>
      <Field label="Candidate"><Select value={choice} onChange={event=>setChoice(Number(event.target.value))}>
        {row.candidates.map((candidate,i)=><option key={i} value={i} disabled={!candidate.eligible}>{candidate.agent} · {candidate.profile||'No profile'} · {candidate.model||'Unavailable'} · {candidate.machine}</option>)}
      </Select></Field>
      <Field label="Reason"><Textarea name="override-reason" required maxLength={1000} value={reason} onChange={event=>setReason(event.target.value)} /></Field>
      <Button type="submit" disabled={change.isPending}>{change.isPending?'Recording…':'Override selection'}</Button>
      {change.error&&<ErrorNote error={change.error}/>}</form>}
  </article>;
}

interface AssignmentReport { version: number; status: string; summary: string; head?: string; handoff_path?: string }
interface AssignmentAttempt { id: string; branch?: string | null; head?: string | null; merged?: boolean; session_id?: string; state: string; reports: AssignmentReport[]; report_versions: number[] }
interface Assignment { id: string; goal: string; state: string; task_type: string; need: {role: string}; attempts: AssignmentAttempt[] }

/** After inspecting an uncertain attempt (a failed launch, a crash), the person marks it failed. */
function FailAttempt({teamId, attempt}: {teamId: string; attempt: AssignmentAttempt}) {
  const client = useQueryClient();
  const [reason, setReason] = useState('');
  const fail = useSingleMutation({mutationFn: () => post(`/api/teams/${teamId}/attempts/${attempt.id}/fail`, {reason}),
    onSuccess: () => { setReason(''); client.invalidateQueries({queryKey: ['selections', teamId]}); }});
  return <details className="mt-1"><summary className="min-h-8 cursor-pointer py-1">Mark attempt failed…</summary>
    <form aria-label={`Mark attempt ${attempt.id} failed`} className="space-y-2" onSubmit={event => { event.preventDefault(); if (reason.trim()) fail.mutate(); }}>
      <p className="text-muted">Use this after checking that the helper isn't running and its checkout has nothing you need. A start that never created its helper gives its count back to the team.</p>
      <Field label="What you checked"><Textarea name="fail-reason" required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></Field>
      <Button type="submit" variant="danger" disabled={fail.isPending || !reason.trim()}>{fail.isPending ? 'Recording…' : 'Mark attempt failed'}</Button>
      {fail.error && <ErrorNote error={fail.error} />}
    </form></details>;
}

function AssignmentRows({rows, teamId}: {rows: Assignment[]; teamId: string}) {
  return <section aria-label="Assignments" className="space-y-3"><h3 className="font-medium">Assignments and attempts</h3>
    {!rows.length && <p className="text-sm text-muted">No assignments recorded.</p>}
    {rows.map(row => <article key={row.id} aria-label={`Assignment ${row.id}`} className="min-w-0 space-y-2 border-t border-line py-3 [overflow-wrap:anywhere]">
      <p className="text-sm font-medium">{row.goal}</p>
      <p className="text-xs text-muted">{words(row.state)} · {words(row.task_type)} · role {row.need.role}</p>
      <p className="break-all text-xs text-muted">{row.id}</p>
      <ol className="space-y-2 pl-4">{row.attempts.map((attempt, index) => <li key={attempt.id} className="text-xs">
        <p>Attempt {index + 1}: {words(attempt.state)} · <span className="break-all">{attempt.id}</span></p>
        {attempt.branch && <p className="break-all">Branch: {attempt.branch}</p>}
        {attempt.head && <p className="break-all">Attempt head: {attempt.head}</p>}
        {attempt.merged && <p>Merge recorded</p>}
        {attempt.session_id && <Link className="text-accent underline" to={`/sessions/${attempt.session_id}`}>Open helper</Link>}
        {['launching', 'running', 'blocked'].includes(attempt.state) && <FailAttempt teamId={teamId} attempt={attempt} />}
        {attempt.reports.map(report => <div key={report.version} className="mt-1 space-y-1">
          <p>Report v{report.version}: {words(report.status)} · {report.summary}</p>
          <p>Kept report versions: {attempt.report_versions.join(', ')}</p>
          {report.head && <p className="break-all">Report head: {report.head}</p>}
          {report.handoff_path && <p className="break-all">Handoff: {report.handoff_path}</p>}
        </div>)}
      </li>)}</ol>
    </article>)}
  </section>;
}

export function SelectionReasons({teamId}: {teamId: string}) {
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [teamId]);
  const query=useQuery({queryKey:['selections',teamId,offset],placeholderData:(previous,previousQuery)=>previousQuery?.queryKey[1]===teamId?previous:undefined,queryFn:()=>api<{selections:Selection[];assignments?:Assignment[];assignments_total?:number}>(`/api/teams/${teamId}/selections?assignment_offset=${offset}`),refetchInterval:3000,retry:(count,error)=>!isNotFound(error)&&count<2});
  if(isNotFound(query.error)) return null;
  return <Card className="space-y-3 p-4"><h2 className="font-medium">Selection reasons</h2>
    <p className="text-xs text-muted">Recorded by Rynhult. The owner's allow-lists can only narrow the limits set when the team started; no choice goes beyond them.</p>
    <QueryState query={query}>{data=><><AssignmentRows rows={data.assignments || []} teamId={teamId}/>
      {!!data.assignments_total && <nav aria-label="Assignment pages" className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Button size="sm" disabled={offset===0 || query.isPlaceholderData} onClick={()=>setOffset(Math.max(0,offset-25))}>Previous assignments</Button>
        <span>{offset+1}–{offset+(data.assignments?.length||0)} of {data.assignments_total}</span>
        <Button size="sm" disabled={offset+25>=data.assignments_total || query.isPlaceholderData} onClick={()=>setOffset(offset+25)}>Next assignments</Button>
      </nav>}
      {data.selections.length?data.selections.map(row=><SelectionRow key={row.id} row={row} teamId={teamId}/>):<p className="text-sm text-muted">No selections recorded for this team.</p>}</>}</QueryState>
    <a className="text-xs text-accent underline" href="/settings#selection-allow-lists">Edit selection allow-lists in Settings</a>
  </Card>;
}

export function SelectionPreferences() {
  const query=useQuery({queryKey:['selection-preferences'],queryFn:()=>api<Policy>('/api/settings/orchestrator/selection'),refetchOnWindowFocus:false,retry:(count,error)=>!isNotFound(error)&&count<2});
  if(isNotFound(query.error)) return null;
  return <Card className="space-y-3 p-4"><h2 id="orchestrator-selection" className="font-medium">Selection allow-lists</h2>
    <p className="text-sm text-muted">For each profile and task type, choose providers and models in preferred order. An exact profile rule replaces the * rule. An empty list denies selection. * in Models allows all models known to Rynhult, including later catalogue additions.</p>
    <QueryState query={query}>{data=><PolicyEditor initial={data}/>}</QueryState></Card>;
}

function PolicyEditor({initial}: {initial:Policy}) {
  const [rules,setRules]=useState(initial.rules);
  const [quota,setQuota]=useState<QuotaPolicy>(initial.quota??{enabled:false,max_used_percent:90,max_age_minutes:10});
  const client=useQueryClient();
  const save=useSingleMutation({mutationFn:()=>api<Policy>('/api/settings/orchestrator/selection',{method:'PUT',json:{version:1,rules,quota}}),
    onSuccess:()=>client.invalidateQueries({queryKey:['selection-preferences']})});
  const patch=(index:number,rule:Rule)=>setRules(current=>current.map((old,i)=>i===index?rule:old));
  return <form className="space-y-4" onSubmit={event=>{event.preventDefault();save.mutate();}}>
    <fieldset className="space-y-3 rounded border border-line p-3"><legend className="text-sm">Provider quota</legend>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={quota.enabled} onChange={e=>setQuota({...quota,enabled:e.target.checked})}/>Use fresh provider quota in selection</label>
      <p className="text-xs text-muted">Off by default. Stale, unknown or unscoped readings never add a block. Owner allow-list order stays first; headroom adds up to 20 points within that order. This is plan usage, not a spending limit.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Skip at usage percent"><Input type="number" required min={1} max={100} step="any" value={quota.max_used_percent} onChange={e=>setQuota({...quota,max_used_percent:Number(e.target.value)})}/></Field>
        <Field label="Quota freshness in minutes"><Input type="number" required min={1} max={60} step={1} value={quota.max_age_minutes} onChange={e=>setQuota({...quota,max_age_minutes:Number(e.target.value)})}/></Field>
      </div>
    </fieldset>
    {rules.map((rule,i)=><fieldset key={i} className="space-y-3 border-t border-line py-3"><legend className="text-sm">Allow-list {i+1}</legend>
      <div className="grid gap-3 sm:grid-cols-2"><Field label={`Profile for rule ${i+1}`}><Input name={`profile-${i}`} value={rule.profile} maxLength={63} spellCheck={false} onChange={e=>patch(i,{...rule,profile:e.target.value})}/></Field>
        <Field label={`Task type for rule ${i+1}`}><Select value={rule.task_type} onChange={e=>patch(i,{...rule,task_type:e.target.value})}>{tasks.map(task=><option key={task} value={task}>{words(task)}</option>)}</Select></Field></div>
      {rule.choices.map((choice,j)=><div key={j} className="space-y-2 rounded border border-line p-3">
        <div className="grid gap-3 sm:grid-cols-2"><Field label={`Provider ${j+1} in rule ${i+1}`}><Input name={`provider-${i}-${j}`} value={choice.agent} spellCheck={false} onChange={e=>patch(i,{...rule,choices:rule.choices.map((c,k)=>k===j?{...c,agent:e.target.value}:c)})}/></Field>
          <Field label={`Models for provider ${j+1} in rule ${i+1}`}><Input name={`models-${i}-${j}`} value={choice.models.join(',')} spellCheck={false} onChange={e=>patch(i,{...rule,choices:rule.choices.map((c,k)=>k===j?{...c,models:e.target.value.split(',').map(v=>v.trim())}:c)})}/></Field></div>
        <div className="flex flex-wrap gap-2"><Button type="button" size="sm" disabled={j===0} aria-label={`Move up provider ${j+1} in rule ${i+1}`} onClick={()=>{const choices=[...rule.choices];[choices[j-1],choices[j]]=[choices[j],choices[j-1]];patch(i,{...rule,choices});}}>Move up</Button>
          <Button type="button" size="sm" aria-label={`Remove provider ${j+1} from rule ${i+1}`} onClick={()=>patch(i,{...rule,choices:rule.choices.filter((_,k)=>k!==j)})}>Remove provider</Button></div></div>)}
      {!rule.choices.length&&<p className="text-sm text-wait">No provider is allowed for this profile and task type.</p>}
      <div className="flex flex-wrap gap-2"><Button type="button" size="sm" onClick={()=>patch(i,{...rule,choices:[...rule.choices,{agent:'claude',models:['*']}]})}>Add provider to rule {i+1}</Button>
        <Button type="button" size="sm" onClick={()=>setRules(rules.filter((_,k)=>k!==i))}>Remove rule {i+1}</Button></div>
    </fieldset>)}
    <div className="flex flex-wrap gap-2"><Button type="button" onClick={()=>setRules([...rules,{profile:'*',task_type:'implementation',choices:[]}])}>Add allow-list</Button>
      <Button type="submit" disabled={save.isPending}>{save.isPending?'Saving…':'Save allow-lists'}</Button></div>
    {save.error&&<ErrorNote error={save.error}/>} {save.isSuccess&&<p role="status" className="text-xs text-muted">Allow-lists saved. Existing grants remain unchanged.</p>}
  </form>;
}
