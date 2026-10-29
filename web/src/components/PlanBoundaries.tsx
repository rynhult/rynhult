/** Person-owned boundaries. Agent proposals are answered on the existing working list. */
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../lib/api';
import type { SessionSummary as Session } from '../lib/types';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { Badge, Button, Card, ErrorNote, Input, Note, QueryState, Select, Textarea } from './ui';

export interface Boundary {
  id: string; text: string; acceptance?: string; source: string; state: string; version: number; brief_version?: number | null;
}
export interface BoundariesPlan { version: number; run_mode?: 'prepare' | 'build'; constraints?: Boundary[]; requirements?: Boundary[] }

export function TeamRunMode({ teamId }: { teamId: string }) {
  const query = useQuery({ queryKey: ['plan', teamId], queryFn: () => api<BoundariesPlan>(`/api/teams/${teamId}/plan`), refetchInterval: 5000 });
  return <Card className="p-4"><QueryState query={query}>{plan => <RunModeControl teamId={teamId} plan={plan} />}</QueryState></Card>;
}

function RunModeControl({ teamId, plan }: { teamId: string; plan: BoundariesPlan }) {
  const client = useQueryClient();
  const [mode, setMode] = useState(plan.run_mode ?? 'build');
  const [progress, setProgress] = useState('');
  const [resumeNeeded, setResumeNeeded] = useState(false);
  const [noticeId, setNoticeId] = useState<string | null>(null);
  useEffect(() => setMode(plan.run_mode ?? 'build'), [plan.run_mode]);
  const notice = useQuery({
    queryKey: ['delivery', noticeId], enabled: !!noticeId,
    queryFn: () => api<{ delivery: { state: string; notice?: string } }>(`/api/deliveries/${noticeId}`),
    refetchInterval: query => ['acknowledged', 'failed', 'expired'].includes(query.state.data?.delivery.state ?? '') ? false : 1500,
  });
  const modeSave = useMutation({
    mutationFn: async (resumeOnly: boolean) => {
      let step = 'Checking the team';
      const advance = (next: string) => { step = next; setProgress(`${next}...`); };
      const waitFor = async (session: Session, stopped: boolean) => {
        const deadline = Date.now() + 30_000;
        while (stopped ? session.desired_state !== 'stopped' || session.team_stop_pending
          : session.desired_state !== 'running' || !['idle', 'active', 'waiting_input'].includes(session.observed_state)) {
          if (Date.now() >= deadline || !stopped && session.observed_state === 'recovery_needed') {
            throw new Error(session.last_error || `The machine has not confirmed ${stopped ? 'the stop' : 'the resumed session'}. Check the lead and retry.`);
          }
          await new Promise(resolve => setTimeout(resolve, 500));
          session = await api<Session>(`/api/sessions/${session.id}`);
        }
      };
      const stop = async (id: string) => waitFor(await api<Session>(`/api/sessions/${id}/stop`, { json: {} }), true);
      const stopHelpers = async () => {
        advance('Stopping helpers and waiting for confirmation');
        const team = await api<{ members: Session[] }>(`/api/sessions/${teamId}/team`);
        for (const member of team.members) {
          if (member.id !== teamId && (member.desired_state === 'running' || member.team_stop_pending)) await stop(member.id);
        }
      };
      try {
        if (!resumeOnly) {
          setResumeNeeded(false); setNoticeId(null);
          if (mode === 'prepare') await stopHelpers();
          advance('Stopping the lead');
          await stop(teamId);
          // A helper could have started while the first set was stopping. The stopped lead cannot start another.
          if (mode === 'prepare') await stopHelpers();
          advance(`Saving ${mode === 'build' ? 'Build' : 'Prepare'} mode`);
          const saved = await api<BoundariesPlan & { run_mode_delivery?: { id: string } }>(`/api/teams/${teamId}/plan`, {
            method: 'PATCH', json: { run_mode: mode, reason: `Person chose ${mode} mode`, version: plan.version },
          });
          client.setQueryData(['plan', teamId], saved);
          setNoticeId(saved.run_mode_delivery?.id ?? null);
          setResumeNeeded(true);
        }
        advance('Resuming the lead');
        await waitFor(await api<Session>(`/api/sessions/${teamId}/resume`, { json: {} }), false);
        setResumeNeeded(false);
        setProgress('Lead resumed with the effective run-mode permissions.');
      } catch (error) {
        setProgress('');
        if (error instanceof ApiError && error.status === 409) {
          const current = (error.data as { plan?: BoundariesPlan } | undefined)?.plan;
          if (current) client.setQueryData(['plan', teamId], current);
        }
        throw new Error(`${step} failed: ${error instanceof Error ? error.message : String(error)} ${step === 'Resuming the lead'
          ? 'The mode is saved and the lead may still be stopped. Use Resume lead to retry.'
          : 'No later steps were run. Review the current plan and session states, then retry.'}`);
      }
    },
    onSettled: () => {
      client.invalidateQueries({ queryKey: ['plan', teamId] });
      client.invalidateQueries({ queryKey: ['team-brief', teamId] });
      client.invalidateQueries({ queryKey: ['team', teamId] });
      client.invalidateQueries({ queryKey: ['session', teamId] });
    },
  });
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Run mode: {(plan.run_mode ?? 'build') === 'prepare' ? 'Prepare' : 'Build'}</h3>
      <p className="text-xs text-muted">Only a signed-in person can widen to Build, even if the brief suggested Prepare. Saving stops the lead, waits for confirmation, changes its permissions and resumes it with a continuation notice. Prepare first stops helpers; they stay stopped. A lead that narrows itself to Prepare stops for read-only permissions to apply on resume.</p>
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); modeSave.mutate(false); }}>
        <Select aria-label="Run mode" value={mode} disabled={modeSave.isPending || resumeNeeded} onChange={(e) => setMode(e.target.value as 'prepare' | 'build')}>
          <option value="prepare">Clarify and prepare</option><option value="build">Build within this scope</option>
        </Select>
        <Button type="submit" size="sm" disabled={modeSave.isPending || resumeNeeded || mode === (plan.run_mode ?? 'build')}>Save run mode</Button>
        {resumeNeeded && !modeSave.isPending && <Button size="sm" onClick={() => modeSave.mutate(true)}>Resume lead</Button>}
      </form>
      {progress && <Note role="status">{progress}{!modeSave.isPending && (noticeId
        ? ` Continuation notice: ${notice.data?.delivery.state ?? 'checking delivery'}. ${notice.data?.delivery.notice ?? ''}`
        : ' Delivery is disabled; give the lead its next instruction in the terminal.')}</Note>}
      {modeSave.error && <ErrorNote error={modeSave.error} />}
      {notice.error && <ErrorNote error={notice.error} title="Could not check the continuation notice" onRetry={() => notice.refetch()} />}
      {notice.data && ['failed', 'expired'].includes(notice.data.delivery.state) && <Note tone="bad" role="alert">
        The mode is saved, but the continuation notice was not confirmed. Check the lead's terminal and delivery history before sending it again.
      </Note>}
    </div>
  );
}

export function PlanBoundaries({ teamId, plan }: { teamId: string; plan: BoundariesPlan }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const done = () => { setEditing(null); client.invalidateQueries({ queryKey: ['plan', teamId] }); client.invalidateQueries({ queryKey: ['worklist', teamId] }); };
  return <section aria-label="Constraints and requirements" className="space-y-4 border-t border-line pt-4">
    <p className="text-xs text-muted">Only you change accepted constraints and requirements. The lead can propose changes as decision cards; reading your text never grants permission to edit it.</p>
    {(['constraints', 'requirements'] as const).map((kind) => <section key={kind} aria-label={kind === 'constraints' ? 'Constraints' : 'Requirements'} className="space-y-2">
      <h3 className="text-sm font-medium">{kind === 'constraints' ? 'Constraints' : 'Requirements'}</h3>
      {(plan[kind] ?? []).map((row) => <div key={row.id} className="space-y-1 rounded-md border border-line p-3">
        <div className="flex flex-wrap items-center gap-2"><span className="text-xs font-medium">{row.id}</span>
          <Badge tone={row.state === 'proposed_change' ? 'wait' : 'neutral'}>{row.state === 'proposed_change' ? 'Proposed change' : row.state}</Badge>
          <span className="text-xs text-muted">{row.state === 'proposed_change' ? 'Awaiting your decision' : row.source === 'brief' ? `Brief version ${row.brief_version}` : 'From you'} · revision {row.version}</span>
        </div>
        <p className="whitespace-pre-wrap break-words text-sm">{row.text}</p>
        {row.acceptance && <p className="whitespace-pre-wrap break-words text-xs text-muted">Acceptance: {row.acceptance}</p>}
        {editing === row.id ? <BoundaryEditor teamId={teamId} version={plan.version} kind={kind} row={row} onDone={done} />
          : <Button size="sm" onClick={() => setEditing(row.id)}>Edit {row.id}</Button>}
      </div>)}
      {!(plan[kind] ?? []).length && <p className="text-xs text-muted">None recorded.</p>}
      {editing === kind ? <BoundaryEditor teamId={teamId} version={plan.version} kind={kind} onDone={done} />
        : <Button size="sm" onClick={() => setEditing(kind)}>Add {kind === 'constraints' ? 'constraint' : 'requirement'}</Button>}
    </section>)}
  </section>;
}

function BoundaryEditor({ teamId, version, kind, row, onDone }: {
  teamId: string; version: number; kind: 'constraints' | 'requirements'; row?: Boundary; onDone: () => void;
}) {
  const client = useQueryClient();
  // The plan version the person started from. The lead updates the plan often while it runs (break-it 14): a newer
  // version is refused, never overwritten; the plan refreshes, the text stays, and saving again uses the latest.
  const [base] = useState(version);
  const [text, setText] = useState(row?.text ?? '');
  const [acceptance, setAcceptance] = useState(row?.acceptance ?? '');
  const [state, setState] = useState(row?.state ?? 'active');
  const [reason, setReason] = useState('');
  const save = useMutation({ mutationFn: (at: number) => api(`/api/teams/${teamId}/plan`, { method: 'PATCH', json: {
    version: at, reason, [kind]: [{ ...(row ? { id: row.id } : {}), text, state,
      ...(kind === 'requirements' ? { acceptance } : {}) }] } }), onSuccess: onDone,
    onError: () => client.invalidateQueries({ queryKey: ['plan', teamId] }) });
  return <form aria-label={`Edit ${row?.id ?? kind}`} className="space-y-2" onSubmit={(e) => { e.preventDefault(); save.mutate(save.error ? version : base); }}>
    <label className="block text-xs">Text<Textarea required maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} className="mt-1 w-full" /></label>
    {kind === 'requirements' && <label className="block text-xs">Acceptance<Textarea maxLength={4000} value={acceptance} onChange={(e) => setAcceptance(e.target.value)} className="mt-1 w-full" /></label>}
    <label className="block text-xs">State<Select value={state} onChange={(e) => setState(e.target.value)} className="ml-2">
      <option value="active">Active</option><option value="retired">Retired</option><option value="proposed_change">Proposed change</option>
    </Select></label>
    <label className="block text-xs">Reason<Input required maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 w-full" /></label>
    <p className="text-xs text-muted">Affected reports and verification will need revalidation.</p>
    <div className="flex gap-2"><Button type="submit" size="sm" disabled={save.isPending}>Save boundary</Button><Button size="sm" onClick={onDone}>Cancel</Button></div>
    {save.error && <ErrorNote error={save.error} />}
  </form>;
}
