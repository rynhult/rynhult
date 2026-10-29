import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, post } from '../lib/api';
import { OFFLINE_TITLE, useOffline, useRole } from '../lib/queries';
import { Badge, Button, Dialog, ErrorNote, Field, Input, Note, QueryState, Select } from './ui';
import { useConfirm } from './confirm';
import { GroupPublished } from './GroupPublished';
import { GroupMachines } from './GroupMachines';

type Group = { id: string; name: string; role: string; revision: number; archived: boolean; remote_execution_available?: boolean };
type Work = { cleanup_pending: boolean; awareness: string; touched_paths: string[]; session_id: string; project_id: number; person: string; machine: string; task: string; branch: string; state: string; stale: boolean; claims: { path: string; expires_at: string }[] };
type Usage = { cost: number | null; coverage: string; usage_warning_usd: number | null; usage_warning_reached: boolean; concurrent: number; daily_launches: number; paused: boolean; limits: { concurrent: number; daily_launches: number }; warning: string };
type Board = { policy_revision: number; revision: number; updated_at: string; rows: Work[]; usage: Usage; advisory: string };

function WorkRow({ row, base, canEdit }: { row: Work; base: string; canEdit: boolean }) {
  const cache = useQueryClient();
  const offline = useOffline();
  const confirm = useConfirm();
  const [task, setTask] = useState(row.task);
  const [path, setPath] = useState('');
  const [context, setContext] = useState('');
  const change = useMutation({ mutationFn: async ({ action, data }: { action: string; data: object }) => {
    const url = `${base}/work/${row.session_id}/${action}`;
    const result = await post<{ status?: string }>(url, data);
    if (result.status === 'conflict' && await confirm({ title: 'Share overlapping work?', body: 'Another session claims this path. Coordinate with its person before editing. Claims do not lock files.', confirm: 'Confirm shared work' })) {
      return post(url, { ...data, shared: true });
    }
    return result;
  }, onSettled: () => { cache.invalidateQueries({ queryKey: ['group-board'] }); } });
  const refresh = useMutation({ mutationFn: () => api<{ context: string }>(`${base}/work/${row.session_id}/context`), onSuccess: data => setContext(data.context) });
  return <article aria-label={`Work by ${row.person}: ${row.task}`} className="space-y-3 rounded-card border border-line p-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><strong className="break-words">{row.task}</strong><Badge>{row.state}</Badge></div>
    {row.cleanup_pending && <Note>A stop or key cleanup is waiting. Rynhult retries when the agent account's session service is back.</Note>}
    <p className="break-words text-xs text-muted">Person: {row.person} · Machine: {row.machine} · Branch: {row.branch}</p>
    <p className="text-xs text-muted">Unpublished; path summary only · Awareness: {row.awareness}{row.stale ? ' · Stale heartbeat' : ''}</p>
    {!!row.touched_paths?.length && <p className="break-words text-xs text-muted">Observed touched paths: {row.touched_paths.join(', ')}</p>}
    <ul className="space-y-1">{row.claims.map(claim => <li key={claim.path} className="flex flex-wrap items-center gap-2 break-all"><span>{claim.path}</span><span className="text-xs text-muted">Expires {new Date(claim.expires_at).toLocaleTimeString()}</span>{canEdit && <Button disabled={offline || change.isPending} title={offline ? OFFLINE_TITLE : undefined} onClick={() => change.mutate({ action: 'release', data: { path: claim.path } })}>Release claim</Button>}</li>)}</ul>
    {canEdit && <>
      <Link to={`/sessions/${row.session_id}`}>Open your session</Link>
      <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); change.mutate({ action: 'task', data: { task } }); }}>
        <Field label="Shared task"><Input name="shared-task" autoComplete="off" required maxLength={500} value={task} onChange={e => setTask(e.target.value)} /></Field><Button type="submit" disabled={offline || change.isPending} title={offline ? OFFLINE_TITLE : undefined}>Update task</Button>
      </form>
      <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); change.mutate({ action: 'claim', data: { path } }); }}>
        <Field label="Claim relative path"><Input name="claim-path" autoComplete="off" required maxLength={300} placeholder="src/api/…" value={path} onChange={e => setPath(e.target.value)} /></Field><Button type="submit" disabled={offline || change.isPending} title={offline ? OFFLINE_TITLE : undefined}>Claim path</Button>
      </form>
      <Button disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh context</Button>
      {context && <pre tabIndex={0} role="region" aria-label="Refreshed board context" className="max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs">{context}</pre>}
    </>}
    <ErrorNote error={change.error || refresh.error} />
  </article>;
}

function Launch({ group, projectId }: { group: Group; projectId: number }) {
  const { username, owner } = useRole();
  const cache = useQueryClient();
  const offline = useOffline();
  const base = `/api/groups/${group.id}/projects/${projectId}`;
  const [placement, setPlacement] = useState('');
  const [commit, setCommit] = useState('');
  const [task, setTask] = useState('');
  const [prompt, setPrompt] = useState('');
  const [provider, setProvider] = useState('claude');
  const [trust, setTrust] = useState(false);
  const [sid, setSid] = useState(() => crypto.randomUUID().replaceAll('-', '').slice(0, 12));
  const [prepared, setPrepared] = useState(false);
  const [launched, setLaunched] = useState('');
  const placements = useQuery({ queryKey: ['group-placements', group.id, projectId], queryFn: () => api<{ id: number; person: string; host: string; trust: string; sync_status: string }[]>(base + '/placements') });
  const refs = useQuery({ queryKey: ['published-refs', group.id, projectId], queryFn: () => api<Record<string, string>>(base + '/exchange/refs') });
  const launch = useMutation({ mutationFn: async () => {
    if (!prepared) {
      await post(base + '/exchange/checkout', { placement_id: Number(placement), session_id: sid, commit });
      setPrepared(true);
    }
    return post<{ id: string }>(base + '/sessions', { checkout_id: sid, task, name: task.slice(0, 40), provider, prompt, trust_source: trust });
  }, onSuccess: result => { setLaunched(result.id); setPrepared(false); setSid(crypto.randomUUID().replaceAll('-', '').slice(0, 12)); cache.invalidateQueries({ queryKey: ['group-board'] }); } });
  const people = useQuery({ queryKey: ['group-people', group.id], enabled: owner, queryFn: () => api<{ id: number; username: string; role: string | null }[]>(`/api/groups/${group.id}/people`) });
  const keys = useQuery({ queryKey: ['group-keys', group.id, projectId], queryFn: () => api<{ key_id: string; attached: boolean }[]>(base + '/keys') });
  const attach = useMutation({ mutationFn: (key: string) => post(base + `/keys/${key}/attach`, {}), onSettled: () => cache.invalidateQueries({ queryKey: ['group-keys'] }) });
  const [person, setPerson] = useState('');
  const approve = useMutation({ mutationFn: async () => {
    const current = await api<{ revision: number }>(`/api/groups/${group.id}`);
    const result = await post<{ id: number }>(base + '/placements', { user_id: Number(person), revision: current.revision });
    return post(base + `/placements/${result.id}/provision`, {});
  }, onSettled: () => { cache.invalidateQueries({ queryKey: ['group-placements'] }); cache.invalidateQueries({ queryKey: ['groups'] }); } });
  return <div className="space-y-4">
    {owner && <form className="space-y-2 border-b border-line pb-3" onSubmit={e => { e.preventDefault(); approve.mutate(); }}>
      <QueryState query={people}>{items => <Field label="Approve private checkout for"><Select required value={person} onChange={e => setPerson(e.target.value)}><option value="">Choose developer…</option>{items.filter(p => p.role === 'member' || p.role === 'maintainer').map(p => <option key={p.id} value={p.id}>{p.username}</option>)}</Select></Field>}</QueryState>
      <Button type="submit" disabled={offline || approve.isPending} title={offline ? OFFLINE_TITLE : undefined}>Approve &amp; provision checkout</Button><ErrorNote error={approve.error} />
    </form>}
    <QueryState query={keys}>{items => <div className="space-y-2"><p className="text-xs text-muted">Eligible group key IDs (values stay private)</p>{items.length ? items.map(key => <div key={key.key_id} className="flex flex-wrap items-center gap-2"><code>{key.key_id}</code><Badge>{key.attached ? 'Attached' : 'Not attached'}</Badge>{(owner || group.role === 'maintainer') && !key.attached && <Button disabled={offline || attach.isPending} title={offline ? OFFLINE_TITLE : undefined} onClick={() => attach.mutate(key.key_id)}>Attach eligible key</Button>}</div>) : <p className="text-xs text-muted">No group keys granted.</p>}</div>}</QueryState><ErrorNote error={attach.error} />
    <form className="space-y-3" onSubmit={e => { e.preventDefault(); launch.mutate(); }}>
      <QueryState query={placements}>{items => <Field label="Your private checkout"><Select required disabled={prepared} value={placement} onChange={e => setPlacement(e.target.value)}><option value="">Choose approved checkout…</option>{items.filter(p => p.person === username && p.trust === 'approved' && p.sync_status === 'ready').map(p => <option key={p.id} value={p.id}>{p.person} · {p.host} · #{p.id}</option>)}</Select></Field>}</QueryState>
      <QueryState query={refs}>{items => <Field label="Reviewed published base"><Select required disabled={prepared} value={commit} onChange={e => { setCommit(e.target.value); setTrust(false); }}><option value="">Choose published commit…</option>{Object.entries(items).map(([ref, sha]) => <option key={ref} value={sha}>{ref} · {sha.slice(0, 12)}</option>)}</Select></Field>}</QueryState>
      <Field label="Group-safe task"><Input name="safe-task" autoComplete="off" required maxLength={500} value={task} onChange={e => setTask(e.target.value)} /></Field>
      <p className="text-xs text-muted">Visible to everyone in this group. Keep credentials and private paths out.</p>
      <Field label="Private opening prompt"><Input name="private-prompt" autoComplete="off" maxLength={20000} value={prompt} onChange={e => setPrompt(e.target.value)} /></Field>
      <Field label="Your agent"><Select value={provider} onChange={e => setProvider(e.target.value)}><option value="claude">Claude Code</option><option value="codex">Codex</option></Select></Field>
      <label className="flex items-start gap-2"><input type="checkbox" checked={trust} onChange={e => setTrust(e.target.checked)} required /><span>I reviewed this published source and trust it to run with my account and eligible keys.</span></label>
      <p className="text-xs text-muted">Read the work board before launching. Your session uses its own checkout on the selected approved machine.</p>
      <Button type="submit" variant="primary" disabled={offline || launch.isPending} title={offline ? OFFLINE_TITLE : undefined}>{launch.isPending ? 'Launching…' : 'Launch group session'}</Button>
      <ErrorNote error={launch.error} />{launched && <p role="status">Session launched. <Link to={`/sessions/${launched}`}>Open session</Link></p>}
    </form>
    {group.remote_execution_available && <GroupMachines group={group} projectId={projectId} />}
  </div>;
}

export function GroupBoard({ group, close }: { group: Group; close: () => void }) {
  const base = `/api/groups/${group.id}`;
  const { username, owner } = useRole();
  const cache = useQueryClient();
  const offline = useOffline();
  const [view, setView] = useState('board');
  const [project, setProject] = useState('');
  const board = useQuery({ queryKey: ['group-board', group.id], queryFn: () => api<Board>(base + '/board'), refetchInterval: 10000 });
  const projects = useQuery({ queryKey: ['group-projects', group.id], queryFn: () => api<{ id: number; name: string; archived: boolean }[]>(base + '/projects') });
  const operations = useQuery({ queryKey: ['group-operations', group.id], queryFn: () => api<{ id: string; summary: string; resolved: boolean; revision: number; assignee: string | null; affected: string[]; acknowledged: string[] }[]>(base + '/operations'), refetchInterval: 10000 });
  const manage = owner || group.role === 'maintainer';
  const people = useQuery({ queryKey: ['group-people', group.id], enabled: manage, queryFn: () => api<{ id: number; username: string; role: string | null }[]>(base + '/people') });
  const [policyRevision, setPolicyRevision] = useState<number | null>(null);
  const [concurrent, setConcurrent] = useState('4');
  const [daily, setDaily] = useState('50');
  const [warning, setWarning] = useState('');
  const [published, setPublished] = useState<{ id: number; name: string } | null>(null);
  useEffect(() => {
    if (view !== 'settings' || !board.data || policyRevision !== null) return;
    setPolicyRevision(board.data.policy_revision);
    setConcurrent(String(board.data.usage.limits.concurrent));
    setDaily(String(board.data.usage.limits.daily_launches));
    setWarning(board.data.usage.usage_warning_usd === null ? '' : String(board.data.usage.usage_warning_usd));
  }, [view, board.data, policyRevision]);
  const policy = useMutation({ mutationFn: () => post(base + '/policy', { revision: policyRevision, concurrent: Number(concurrent), daily_launches: Number(daily), usage_warning_usd: warning ? Number(warning) : null }), onSuccess: () => setPolicyRevision(value => value === null ? null : value + 1), onSettled: () => { cache.invalidateQueries({ queryKey: ['group-board'] }); cache.invalidateQueries({ queryKey: ['groups'] }); } });
  const resolve = useMutation({ mutationFn: ({ id, data }: { id: string; data: { revision: number; action: string; user_id?: number | null } }) => post(base + `/operations/${id}/update`, data), onSettled: () => cache.invalidateQueries({ queryKey: ['group-operations'] }) });
  return <Dialog open onClose={close} title={`${group.name} · Work board`} dismissible={false} description="Explicit shared tasks and advisory path claims. Private prompts, terminals and unfinished diffs stay personal.">
    <div className="space-y-4">
      <Field label="Group view"><Select value={view} onChange={e => { setView(e.target.value); if (e.target.value === 'settings') setPolicyRevision(null); }}><option value="board">Work board &amp; Sessions</option><option value="launch">New group session</option><option value="changes">Published changes &amp; Files</option><option value="mine">Assigned to me</option><option value="inbox">Group operations</option><option value="settings">Usage &amp; Settings</option></Select></Field>
      <QueryState query={board}>{data => <>
        <p className="text-xs text-muted">Revision {data.revision} · Refreshed {new Date(data.updated_at).toLocaleTimeString()} · {data.usage.concurrent} active · {data.usage.daily_launches} launches today</p>
        {data.usage.paused && <Note>Group launches paused. Ask a maintainer or instance owner to review and override the pause.</Note>}
        <p className="text-xs text-muted">{data.advisory}</p>
        {view === 'board' && (data.rows.length ? data.rows.map(row => <WorkRow key={row.session_id} row={row} base={base} canEdit={!group.archived && (owner || row.person === username) && group.role !== 'viewer'} />) : <p>No group sessions yet. Review published source, then launch with a group-safe task.</p>)}
        {view === 'settings' && <><p>{data.usage.warning}</p><p>Reported usage: {data.usage.cost === null ? 'Unknown' : new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(data.usage.cost)} · Coverage: {data.usage.coverage}</p>{data.usage.usage_warning_reached && <Note>Reported usage reached the group warning threshold. Running sessions continue.</Note>}<p>Concurrent quota: {data.usage.limits.concurrent} · Daily launch quota: {data.usage.limits.daily_launches}</p>{manage && !group.archived && policyRevision !== null && <form className="space-y-3" onSubmit={e => { e.preventDefault(); policy.mutate(); }}><Field label="Concurrent sessions"><Input type="number" name="concurrent" min={1} max={100} value={concurrent} onChange={e => setConcurrent(e.target.value)} /></Field><Field label="Daily launches"><Input type="number" name="daily-launches" min={1} max={10000} value={daily} onChange={e => setDaily(e.target.value)} /></Field><Field label="Reported USD warning"><Input type="number" name="usage-warning" min={0} max={1000000} step="0.01" value={warning} onChange={e => setWarning(e.target.value)} /></Field><Button type="submit" disabled={offline || policy.isPending} title={offline ? OFFLINE_TITLE : undefined}>Save launch quotas</Button><ErrorNote error={policy.error} /></form>}</>}
      </>}</QueryState>
      {view === 'launch' && !group.archived && group.role !== 'viewer' && <><QueryState query={projects}>{items => <Field label="Group project"><Select required value={project} onChange={e => setProject(e.target.value)}><option value="">Choose project…</option>{items.filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</Select></Field>}</QueryState>{project && <Launch key={project} group={group} projectId={Number(project)} />}</>}
      {view === 'changes' && <QueryState query={projects}>{items => items.map(p => <div key={p.id} className="flex flex-wrap items-center justify-between gap-2"><span>{p.name}</span><Button onClick={() => setPublished(p)}>Published Files &amp; Wiki</Button></div>)}</QueryState>}
      {published && <GroupPublished groupId={group.id} project={published} close={() => setPublished(null)} />}
      {(view === 'inbox' || view === 'mine') && <QueryState query={operations}>{allItems => { const items = view === 'mine' ? allItems.filter(item => item.assignee === username) : allItems; return items.length ? items.map(item => <article key={item.id} className="space-y-2 border-b border-line py-3"><p>{item.summary}</p><Badge>{item.resolved ? 'Resolved' : 'Needs review'}</Badge><p className="text-xs text-muted">Assigned to: {item.assignee ?? 'Unassigned'} · Revision {item.revision}</p>
        {!item.resolved && item.affected?.includes(username ?? '') && !item.acknowledged?.includes(username ?? '') && <Button disabled={offline || resolve.isPending} title={offline ? OFFLINE_TITLE : undefined} onClick={() => resolve.mutate({ id: item.id, data: { revision: item.revision, action: 'ack' } })}>Acknowledge my conflict</Button>}
        {manage && !item.resolved && <><Button disabled={offline || resolve.isPending} title={offline ? OFFLINE_TITLE : undefined} onClick={() => resolve.mutate({ id: item.id, data: { revision: item.revision, action: 'resolve' } })}>Resolve operation</Button>
          <QueryState query={people}>{members => <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); const selected = new FormData(e.currentTarget).get('assignee'); resolve.mutate({ id: item.id, data: { revision: item.revision, action: 'assign', user_id: selected ? Number(selected) : null } }); }}><Field label="Assign operation"><Select name="assignee" defaultValue={members.find(person => person.username === item.assignee)?.id ?? ''}><option value="">Unassigned</option>{members.filter(person => person.role === 'maintainer').map(person => <option key={person.id} value={person.id}>{person.username}</option>)}</Select></Field><Button type="submit" disabled={offline || resolve.isPending} title={offline ? OFFLINE_TITLE : undefined}>Save assignee</Button></form>}</QueryState></>}</article>) : <p>No group operations.</p>; }}</QueryState>}
      <ErrorNote error={resolve.error} />
    </div>
  </Dialog>;
}
