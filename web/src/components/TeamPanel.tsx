import { ProjectGatesCard } from './Gates';
import { useLocation, useNavigate } from 'react-router-dom';
import { TeamSection, teamPanelSections } from './TeamSection';
import { TeamRunMode } from './PlanBoundaries';
import { TeamBrief } from './TeamBrief';
import { permissionSummary } from "../lib/words";
import { providerShort, useInstance } from "../lib/queries";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post } from '../lib/api';
import type { SessionSummary } from '../lib/types';
import { stamp, until } from '../lib/format';
import { Button, Card, ErrorNote, QueryState, Tabs, TabPanel } from './ui';
import { Plan } from './Plan';
import { SessionList } from './SessionList';
import { SelectionReasons } from './SelectionReasons';
import { TeamTree } from './TeamTree';
import { WorkList } from './WorkList';
import { useConfirm } from './confirm';

interface Snapshot {
  reservations?: { id: number; kind: string; label: string; state: string; branch: string | null; expires_at: string }[];
  limits: { at_once: number; total: number; depth: number; hours: number; machines: string[]; agents: { agent: string; profile: string | null; inputs: object }[] };
  used: { started: number; running: number }; expires_at: string; stopped: boolean;
  wrapup_until?: string | null; members: (SessionSummary & { stop_state?: string | null; verification?: { gate?: { state: string; stale?: string | false | null } | null; review?: { status: string } } })[]; decisions: { action: string; ts: string; detail: Record<string, unknown> }[];
}
export function TeamPanel({ id, projectId }: { id: string; projectId: number }) {
  const location = useLocation();
  const navigate = useNavigate();
  const anchor = location.hash.slice(1);
  const section = anchor === 'worklist' ? 'worklist' : anchor === 'settings' ? 'settings'
    : Object.hasOwn(teamPanelSections, anchor) ? teamPanelSections[anchor] : 'overview';
  const tabs = [{ id: 'overview', label: 'Overview' }, { id: 'worklist', label: 'Working list' }, { id: 'settings', label: 'Settings' }];
  const { host, orchestrator } = useInstance();
  const client = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({ queryKey: ['team', id], queryFn: () => api<Snapshot>(`/api/sessions/${id}/team`), refetchInterval: 3000 });
  const stop = useMutation({ mutationFn: () => post(`/api/sessions/${id}/team/stop`), onSuccess: () => {
    client.invalidateQueries({ queryKey: ['team', id] }); client.invalidateQueries({ queryKey: ['sessions'] });
  } });
  // Observe the children's existing query keys: no additional polling or network requests.
  const plan = useQuery<{ run_mode?: string; state: string; version: number }>({ queryKey: ['plan', id], enabled: false });
  const brief = useQuery<{ version: number } | null>({ queryKey: ['team-brief', id], enabled: false });
  const gates = useQuery<{ budget: { enabled: boolean }; running: number; jobs: { state: string }[] }>({ queryKey: ['gates', projectId], enabled: false });
  const planSummary = plan.error ? 'Plan refresh failed; expand for details.' : plan.data ? `${plan.data.run_mode === 'prepare' ? 'Prepare' : 'Build'} · ${plan.data.state} · version ${plan.data.version}` : 'Loading plan…';
  const gateSummary = gates.error ? 'Gate refresh failed; expand for details.' : gates.data ? gates.data.budget.enabled ? `${gates.data.running} host slots used · ${gates.data.jobs.filter(j => j.state === 'queued').length} queued shown` : 'Gate queue is off' : 'Loading gate status…';
  const SettingsPanel = orchestrator ? TabPanel : 'div';
  return <QueryState query={query}>{(team) => <div className="space-y-4">
    {orchestrator && <Tabs id={`team-sections-${id}`} label="Team sections" tabs={tabs} value={section}
      onChange={value => navigate({ pathname: location.pathname, search: location.search, hash: `#${value}` }, { state: { teamTabNavigation: true } })} />}
    {orchestrator && section === 'overview' && <TabPanel id={`team-sections-${id}`} value={section} className="space-y-4">
      <TeamSection teamId={id} panel="brief" title="Project brief" summary={brief.error ? 'Brief refresh failed; expand for details.' : brief.data ? `Pinned version ${brief.data.version}` : brief.isPending ? 'Loading brief…' : 'No pinned project brief'}><TeamBrief teamId={id} projectId={projectId} /></TeamSection>
      <TeamSection teamId={id} panel="plan" title="Plan" summary={planSummary}><Plan teamId={id} /></TeamSection>
    </TabPanel>}
    {orchestrator && section === 'worklist' && <TabPanel id={`team-sections-${id}`} value={section}>
      <WorkList teamId={id} />
    </TabPanel>}
    {(!orchestrator || section === 'settings') && <SettingsPanel id={`team-sections-${id}`} value="settings" className="space-y-4">
      {orchestrator && <><TeamSection teamId={id} panel="run-mode" title="Run mode" summary={planSummary}><TeamRunMode teamId={id} /></TeamSection>
        <TeamSection teamId={id} panel="agent-tree" title="Live agent tree" summary={`Helpers: ${team.used.running} running · ${team.used.started} started`}><TeamTree teamId={id} wrapupUntil={team.wrapup_until} /></TeamSection>
        <TeamSection teamId={id} panel="gate-queue" title="Gate queue" summary={gateSummary}><ProjectGatesCard project={{ id: projectId }} /></TeamSection></>}
    {orchestrator && <TeamSection teamId={id} panel="reservations" title="Number reservations" summary={`Reservations recorded: ${team.reservations?.length ?? 0}`}><Card className="p-4"><section aria-label="Number reservations" className="space-y-3">
      <h2 className="font-medium">Number reservations</h2>
      {!team.reservations?.length ? <p className="text-xs text-muted">No migration or decision numbers reserved by this team.</p>
        : <ul className="space-y-2 text-xs">{team.reservations.map(row => <li key={row.id} className="break-words">
          {row.kind} {row.label} · {row.state === 'reserved' && new Date(row.expires_at).getTime() <= Date.now() ? 'expired' : row.state}
          {row.branch && <> · {row.branch}</>}{row.state === 'reserved' && <> · Expires {stamp(row.expires_at)}</>}
        </li>)}</ul>}
    </section></Card></TeamSection>}
    <TeamSection teamId={id} panel="team-limits" title="Team limits" summary={`${team.wrapup_until ? 'Expiry wrap-up' : team.members.some(s => s.stop_state === 'stop requested') ? 'Stop requested' : team.stopped ? 'Stop confirmed' : 'Active'} · ${team.used.running}/${team.limits.at_once} helpers running`}>
    <Card className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-medium">{team.wrapup_until ? 'Team expiry wrap-up' : team.members.some(s => s.stop_state === 'stop requested') ? 'Stop requested' : team.stopped ? 'Stop confirmed' : 'Team limits'}</h2>
        <Button variant="danger" disabled={(team.stopped && !team.wrapup_until && !team.members.some(s => s.stop_state === "stop requested")) || stop.isPending} onClick={async () => {
          if (await confirm({ title: 'Stop team?', body: 'Stop the lead and every helper. No more helpers can start under this grant.', confirm: 'Stop team', danger: true })) stop.mutate();
        }}>Stop team</Button></div>
      {team.wrapup_until && <p className="text-xs text-wait">Lead wrap-up ends {stamp(team.wrapup_until)}. Only reports, plan/working-list updates and finishing stopped helpers are allowed.</p>}
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4 tnum">
        <div><dt className="text-xs text-muted">Helpers running</dt><dd>{team.used.running} / {team.limits.at_once}</dd></div>
        <div><dt className="text-xs text-muted">Helpers started</dt><dd>{team.used.started} / {team.limits.total}</dd></div>
        <div><dt className="text-xs text-muted">Depth</dt><dd>{team.limits.depth}</dd></div>
        <div><dt className="text-xs text-muted">Time left</dt><dd>{team.stopped ? 'Stopped' : until(new Date(team.expires_at).getTime() / 1000)}</dd></div>
      </dl>
      <p className="text-xs text-muted">Machines: {team.limits.machines.map(m => m === 'local' ? `${host} · main` : m).join(', ')} · Time limit: {team.limits.hours} {team.limits.hours === 1 ? 'hour' : 'hours'} · Counts from Rynhult</p>
      <ul className="space-y-1 text-xs text-muted">{team.limits.agents.map((choice, i) => {
        const inputs = choice.inputs as Record<string, unknown>;
        const mode = permissionSummary(choice.agent, inputs);
        return <li key={i} title={JSON.stringify(choice.inputs)}>{providerShort(choice.agent)} · {choice.profile || 'No profile'} · {String(mode || 'Provider defaults')}</li>;
      })}</ul>
      {stop.error && <ErrorNote error={stop.error} />}
    </Card></TeamSection>
    <TeamSection teamId={id} panel="team-members" title="Team members" summary={`Agents: ${team.members.length} · waiting for input: ${team.members.filter(s => s.observed_state === 'waiting_input').length}`}>
    <Card><SessionList sessions={team.members} />
      {team.members.filter(s => s.verification).map(s => <p key={s.id} className="break-words px-4 py-2 text-xs text-muted">
        {s.name}: {s.verification?.review?.status ?? 'not reviewed at this revision'}
        {s.verification?.gate && <> · gate {s.verification.gate.state}{s.verification.gate.stale ? ` · stale: ${s.verification.gate.stale}` : s.verification.gate.stale === false ? '' : ' · freshness unconfirmed'}</>}
      </p>)}
      {team.members.some((s) => s.git?.error) && <p className="p-4 text-xs text-wait">Some branch information could not be read. Retry when its machine is connected.</p>}
      {team.members.some((s) => s.git?.lfs) && <p className="p-4 text-xs text-wait">LFS files arrive as pointers; run git lfs checkout</p>}
      {team.members.filter((s) => s.git?.commits?.length).map((s) => <details key={s.id} className="border-t border-line p-4 text-xs">
        <summary className="cursor-pointer">{s.name}: {s.git?.ahead} commits since launch</summary>
        <ul className="mt-2 space-y-1">{s.git?.commits?.map((c) => <li key={c.sha}><span className="font-mono text-muted">{c.sha.slice(0, 8)}</span> {c.subject}</li>)}</ul>
      </details>)}
    </Card></TeamSection>
    <TeamSection teamId={id} panel="decision-log" title="Decision log" summary={`Recorded events: ${team.decisions.length}`}>
    <Card className="p-4"><h2 className="mb-3 font-medium">Decision log</h2>
      <ol className="space-y-3">{team.decisions.map((entry, i) => <li key={i} className="text-xs">
        <span className="text-muted">{stamp(entry.ts)} · </span>{({ 'team.grant': 'Role granted', 'team.start': 'Helper started', 'team.merge': 'Branch merge', 'team.stop': 'Stopped', 'team.finish': 'Helper finished', 'team.message': 'Message sent', 'team.expired': 'Time limit reached' } as Record<string, string>)[entry.action] ?? entry.action}
        {entry.detail.name ? <span> · {String(entry.detail.name)}</span> : null}
        {entry.detail.helper ? <span> · {String(entry.detail.helper)}</span> : null}
        {entry.detail.task ? <p className="mt-1 whitespace-pre-wrap break-words text-muted">{String(entry.detail.task)}</p> : null}
        {entry.detail.text ? <p className="mt-1 whitespace-pre-wrap break-words text-muted">{String(entry.detail.text)}</p> : null}
        {entry.detail.conflicts instanceof Array && entry.detail.conflicts.length > 0 ? <p className="text-wait">Conflicts: {entry.detail.conflicts.join(', ')}</p> : null}
      </li>)}</ol>
    </Card></TeamSection>
    {orchestrator && <TeamSection teamId={id} panel="selection-reasons" title="Selection reasons" summary="Agent choices, assignments and overrides"><SelectionReasons teamId={id} /></TeamSection>}
    </SettingsPanel>}
  </div>}</QueryState>;
}
