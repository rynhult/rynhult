/** Orchestrator v2, slice 6: the team's plan and roadmap (src/acc/server/orchestrator/planning.py). Mounted on the Team
 *  tab above the working list; renders nothing while the server has orchestrator v2 off (the plan answers 404). The
 *  lead writes the plan; the person reads it, answers its questions on the working list and can edit it here. Each
 *  edit is a new version under their name. The lead's text is shown as plain text, never as HTML, and the roadmap
 *  diagram is rendered by MermaidDiagram (strict, sanitized SVG). */
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, isNotFound } from '../lib/api';
import { stamp } from '../lib/format';
import { clearDraft, loadDraft, saveDraft } from '../lib/drafts';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { PlanBoundaries, type BoundariesPlan } from './PlanBoundaries';
import { MermaidDiagram } from './MermaidDiagram';
import { Badge, Button, Card, CardHeader, ErrorNote, Input, Note, Select, StaleNote, Textarea } from './ui';

type PhaseStatus = 'planned' | 'active' | 'done' | 'dropped';
interface Phase { inactive_requirements?: string[]; decision_blocks?: { id: number; title: string }[]; requirement_ids?: string[]; key: string; title: string; summary: string; check: string; status: PhaseStatus; depends_on: string[]; blocked_by: string[] }
interface Scope { in: string[]; out: string[]; parked: string[] }
interface PlanView extends BoundariesPlan {
  markdown?: string; state: 'planning' | 'ready'; idea: string; version: number; ready_at: string | null; scope: Scope; phases: Phase[];
  person_edits?: Record<string, { version: number; by: string }>;
  open_questions: { id: number; kind: string; title: string }[]; mermaid: string;
  history: { version: number; reason: string; actor: string; at: string }[];
}

const STATUS_WORDS: Record<PhaseStatus, string> = { planned: 'Planned', active: 'In progress', done: 'Done', dropped: 'Dropped' };
const STATUS_TONE: Record<PhaseStatus, 'ok' | 'info' | 'neutral'> = { planned: 'neutral', active: 'info', done: 'ok', dropped: 'neutral' };
const actorWords = (actor: string) => actor.startsWith('session:') ? 'the lead' : actor;

export function Plan({ teamId }: { teamId: string }) {
  const client = useQueryClient();
  const key = ['plan', teamId];
  const query = useQuery({
    queryKey: key, queryFn: () => api<PlanView>(`/api/teams/${teamId}/plan`), refetchInterval: 5000,
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  // An unsaved edit kept for this tab (a refresh or Back) reopens the editor with it.
  const [editing, setEditing] = useState(() => loadDraft(`plan:${teamId}`) !== null);
  if (query.error && isNotFound(query.error)) return null;
  if (!query.data) return query.error ? <ErrorNote error={query.error} title="Couldn't load the plan" onRetry={() => query.refetch()} /> : null;
  const plan = query.data;
  const titles = Object.fromEntries(plan.phases.map((p) => [p.key, p.title]));
  const edited = (field: string) => {
    const by = plan.person_edits?.[field];
    return by ? <Badge>Edited by {by.by} · version {by.version}</Badge> : null;
  };
  const scope = (title: string, lines: string[]) => <div>
    <h4 className="text-xs text-muted">{title}</h4>
    {lines.length ? <ul className="list-disc space-y-0.5 pl-5 text-sm">{lines.map((line, i) => <li key={i} className="break-words">{line}</li>)}</ul>
      : <p className="text-sm text-muted">None</p>}
  </div>;
  const heading = (text: string, badge?: React.ReactNode) => <div className="flex flex-wrap items-center gap-2">
    <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{text}</h3>{badge}</div>;
  return <Card>
    <CardHeader title="Plan" subtitle={plan.state === 'planning'
      ? 'Planning session: no helpers start until the lead finishes the plan.'
      : `Version ${plan.version}. Answer its open questions on the working list, in any order, or edit it here.`} />
    <div className="space-y-5 p-4">
      {query.error && <StaleNote error={query.error} />}
      <section aria-label="Effective run mode" className="space-y-1">
        <h3 className="text-sm font-semibold">Effective run mode: {plan.run_mode === 'prepare' ? 'Prepare' : 'Build'}</h3>
        <p className="text-xs text-muted">Plan controls this mode, even when the pinned brief suggested something different. Only you can widen to Build; the lead can narrow to Prepare.</p>
      </section>
      {plan.state === 'planning' && <Note tone="wait" role="status">The lead is still planning with you. Talk to it in its terminal, answer its questions on the working list, or edit the plan.</Note>}
      {editing
        ? <PlanEditor teamId={teamId} plan={plan} onDone={() => {
          setEditing(false);
          client.invalidateQueries({ queryKey: key });
        }} onCancel={() => setEditing(false)} />
        : <>
          <div className="flex flex-wrap justify-end gap-2">
            <Button size="sm" disabled={!plan.markdown} onClick={() => {
              const url = URL.createObjectURL(new Blob([plan.markdown ?? ''], { type: 'text/markdown;charset=utf-8' }));
              const link = document.createElement('a'); link.href = url; link.download = 'ROADMAP.md';
              link.click(); URL.revokeObjectURL(url);
            }}>Export roadmap</Button>
            <Button size="sm" onClick={() => setEditing(true)}>Edit plan</Button>
          </div>
          <section aria-label="Idea" className="space-y-1">
            {heading('Idea', edited('idea'))}
            <p className="whitespace-pre-wrap break-words text-sm">{plan.idea || <span className="text-muted">Not written yet.</span>}</p>
          </section>
          <section aria-label="Scope" className="space-y-2">
            {heading('Scope', edited('scope'))}
            <div className="grid gap-3 sm:grid-cols-3">{scope('In scope', plan.scope.in)}{scope('Out of scope', plan.scope.out)}{scope('Parked for later', plan.scope.parked)}</div>
          </section>
          <section aria-label="Roadmap" className="space-y-2">
            {heading('Roadmap')}
            {plan.phases.length ? <ol className="space-y-2">{plan.phases.map((phase) => <li key={phase.key} aria-label={phase.title}
              className="space-y-1 rounded-md border border-line p-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={STATUS_TONE[phase.status]}>{STATUS_WORDS[phase.status]}</Badge>
                {phase.status !== 'done' && phase.status !== 'dropped' && (phase.blocked_by.length || phase.decision_blocks?.length || phase.inactive_requirements?.length
                  ? <Badge tone="wait">Waits for {[...phase.blocked_by.map((k) => titles[k] ?? k), ...(phase.inactive_requirements ?? []).map((id) => `active ${id}`), ...(phase.decision_blocks ?? []).map((c) => `decision ${c.id}: ${c.title}`)].join(', ')}</Badge>
                  : <Badge tone="ok">Ready to start</Badge>)}
                {edited(`phase:${phase.key}`)}
              </div>
              <h4 className={`break-words text-sm font-medium ${phase.status === 'dropped' ? 'line-through' : ''}`}>{phase.title}</h4>
              {phase.summary && <p className="whitespace-pre-wrap break-words text-sm text-muted">{phase.summary}</p>}
              {!!phase.requirement_ids?.length && <p className="text-xs text-muted">Requirements: {phase.requirement_ids.join(", ")}</p>}
              {phase.depends_on.length > 0 && <p className="text-xs text-muted">After: {phase.depends_on.map((k) => titles[k] ?? k).join(', ')}</p>}
              {phase.check && <p className="text-xs text-muted">Checked by: {phase.check}</p>}
            </li>)}</ol> : <p className="text-xs text-muted">No phases yet.</p>}
          </section>
        </>}
      <PlanBoundaries teamId={teamId} plan={plan} />
      <section aria-label="Open questions" className="space-y-1">
        {heading('Open questions')}
        {plan.open_questions.length
          ? <ul className="list-disc space-y-0.5 pl-5 text-sm">{plan.open_questions.map((q) => <li key={q.id} className="break-words">{q.title}</li>)}</ul>
          : <p className="text-xs text-muted">None waiting for you.</p>}
      </section>
      {plan.history.length > 0 && <details className="text-sm">
        <summary className="cursor-pointer text-xs text-muted">Plan history ({plan.history.length} {plan.history.length === 1 ? 'version' : 'versions'})</summary>
        <ol className="mt-2 space-y-1">{plan.history.map((h) => <li key={h.version} className="text-xs">
          <span className="tnum font-medium">Version {h.version}</span><span className="text-muted"> · {actorWords(h.actor)} · {stamp(h.at)} · </span>
          <span className="break-words">{h.reason}</span>
        </li>)}</ol>
      </details>}
      {plan.phases.length > 0 && <section aria-label="Dependency diagram" className="space-y-2">
        {heading('Dependency diagram')}
        <MermaidDiagram source={plan.mermaid}
          label={`Roadmap diagram: ${plan.phases.length} phases and what each waits for, as listed above.`} />
      </section>}
      {plan.phases.length > 0 && <details className="text-sm">
        <summary className="cursor-pointer text-xs text-muted">Diagram source (Mermaid, as the lead commits it in docs/ROADMAP.md)</summary>
        <pre className="mt-2 overflow-x-auto rounded-md bg-panel-2 p-3 text-xs">{plan.mermaid}</pre>
      </details>}
    </div>
  </Card>;
}

interface DraftPhase { requirement_ids?: string[]; key: string; title: string; summary: string; check: string; status: PhaseStatus; depends_on: string[]; isNew?: boolean }
interface PlanDraft {
  origin: PlanView; base: number; latest: PlanView | null;
  idea: string; scope: { in: string; out: string; parked: string }; phases: DraftPhase[]; reason: string;
}

/** What changed in the plan between two versions, in words, with the newer values (for a conflict). */
function changedBetween(before: PlanView, after: PlanView): string[] {
  const out: string[] = [];
  if (before.idea !== after.idea) out.push(`Idea is now: ${after.idea || '(empty)'}`);
  for (const [part, words] of [['in', 'In scope'], ['out', 'Out of scope'], ['parked', 'Parked for later']] as const) {
    if (JSON.stringify(before.scope[part]) !== JSON.stringify(after.scope[part])) {
      out.push(`${words} is now: ${after.scope[part].join('; ') || '(none)'}`);
    }
  }
  const old = Object.fromEntries(before.phases.map((p) => [p.key, p]));
  for (const phase of after.phases) {
    const was = old[phase.key];
    if (!was) { out.push(`New phase: ${phase.title} (${STATUS_WORDS[phase.status]})`); continue; }
    const parts = [
      was.title !== phase.title && `title "${phase.title}"`,
      was.status !== phase.status && `status ${STATUS_WORDS[phase.status]}`,
      was.summary !== phase.summary && `summary "${phase.summary}"`,
      was.check !== phase.check && `check "${phase.check}"`,
      JSON.stringify(was.depends_on) !== JSON.stringify(phase.depends_on) && `comes after ${phase.depends_on.join(', ') || 'nothing'}`,
      JSON.stringify(was.requirement_ids ?? []) !== JSON.stringify(phase.requirement_ids ?? []) && `requirements ${(phase.requirement_ids ?? []).join(', ') || 'none'}`,
    ].filter(Boolean);
    if (parts.length) out.push(`Phase ${was.title}: now ${parts.join(', ')}`);
  }
  return out;
}

function PlanEditor({ teamId, plan, onDone, onCancel }: {
  teamId: string; plan: PlanView; onDone: () => void; onCancel: () => void;
}) {
  const lines = (text: string) => text.split('\n').map((l) => l.trim()).filter(Boolean);
  const storeKey = `plan:${teamId}`;
  const [restored] = useState(() => loadDraft<PlanDraft>(storeKey));
  // The plan as it was when editing began: what the person's changes are measured against. The live plan keeps
  // refreshing underneath; measuring against it (or sending its version) would quietly undo a save made meanwhile in
  // another tab or by the lead (break-it review 2026-10-26). `base` is the version this edit is sent against: after a
  // conflict, the latest one, so the person's changes (and only theirs) are applied on top of it, never discarded.
  const [origin] = useState<PlanView>(restored?.origin ?? plan);
  const [latest, setLatest] = useState<PlanView | null>(plan.version !== origin.version ? plan : null);
  const base = latest?.version ?? origin.version;
  useEffect(() => {
    if (plan.version > base) setLatest(plan);
  }, [plan, base]);
  const [idea, setIdea] = useState(restored?.idea ?? origin.idea);
  const [scope, setScope] = useState(restored?.scope ?? { in: origin.scope.in.join('\n'), out: origin.scope.out.join('\n'), parked: origin.scope.parked.join('\n') });
  const [phases, setPhases] = useState<DraftPhase[]>(restored?.phases ?? origin.phases.map(({ blocked_by: _, decision_blocks: __, inactive_requirements: ___, ...p }) => p));
  const [reason, setReason] = useState(restored?.reason ?? '');
  const changes = () => {
    // Only what the person changed, so an edit never claims (or blocks the lead on) fields they didn't touch.
    const body: Record<string, unknown> = {};
    if (idea !== origin.idea) body.idea = idea;
    const changedScope = (['in', 'out', 'parked'] as const).filter(part =>
      JSON.stringify(lines(scope[part])) !== JSON.stringify(origin.scope[part]));
    if (changedScope.length) body.scope = {
      ...(latest ?? origin).scope, ...Object.fromEntries(changedScope.map(part => [part, lines(scope[part])])),
    };
    const before = Object.fromEntries(origin.phases.map((p) => [p.key, p]));
    const changed = phases.flatMap((p) => {
      const old = before[p.key];
      if (!old) { const { isNew: _, ...added } = p; return [added]; }
      const fields = (['title', 'summary', 'check', 'status', 'depends_on', 'requirement_ids'] as const)
        .filter(field => JSON.stringify(old[field] ?? []) !== JSON.stringify(p[field] ?? []));
      return fields.length ? [{ key: p.key, ...Object.fromEntries(fields.map(field => [field, p[field]])) }] : [];
    });
    if (changed.length) body.phases = changed;
    return body;
  };
  const dirty = Object.keys(changes()).length > 0 || reason.trim() !== '';
  useEffect(() => {
    // Kept for this tab while there is something to lose; a refresh or Back brings it back.
    if (dirty || latest) saveDraft(storeKey, { origin, base, latest, idea, scope, phases, reason } satisfies PlanDraft);
    else clearDraft(storeKey);
  }, [storeKey, dirty, origin, base, latest, idea, scope, phases, reason]);
  const finish = (done: () => void) => { clearDraft(storeKey); done(); };
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api(`/api/teams/${teamId}/plan`, { method: 'PATCH', json: {
      ...body, ...(reason.trim() ? { reason: reason.trim() } : {}), version: base } }),
    onSuccess: () => finish(onDone),
    onError: async (error) => {
      if (!(error instanceof ApiError && error.status === 409)) return;
      try {
        const now = (error.data as { plan?: PlanView } | undefined)?.plan ?? await api<PlanView>(`/api/teams/${teamId}/plan`);
        if (now.version !== base) setLatest(now);
      } catch { /* the error note below says the save failed; the draft stays */ }
    },
  });
  const set = (index: number, change: Partial<DraftPhase>) => setPhases(phases.map((p, i) => i === index ? { ...p, ...change } : p));
  const addPhase = () => {
    const used = new Set(phases.map((p) => p.key));
    let n = phases.length + 1;
    while (used.has(`phase-${n}`)) n += 1;
    setPhases([...phases, { key: `phase-${n}`, title: '', summary: '', check: '', status: 'planned', depends_on: [], isNew: true }]);
  };
  const meanwhile = latest ? changedBetween(origin, latest) : [];
  return <form aria-label="Edit the plan" className="space-y-4" onSubmit={(e) => {
    e.preventDefault();
    const body = changes();
    if (Object.keys(body).length) save.mutate(body); else finish(onCancel);  // nothing changed: no empty version
  }}>
    {latest && <Note tone="wait" role="status">
      <p>The plan changed while you were editing: it is now version {latest.version}
        {latest.history[0] ? `, by ${actorWords(latest.history[0].actor)}` : ''}. Your edits are kept below.</p>
      {meanwhile.length > 0 && <ul className="mt-1 list-disc space-y-0.5 pl-5">{meanwhile.map((line, i) => <li key={i} className="break-words">{line}</li>)}</ul>}
      <p className="mt-1">Save as a new version applies your edits on top of it; where you both changed the same thing, yours is saved.</p>
    </Note>}
    {restored && !latest && <Note role="status">Restored your unsaved edit.</Note>}
    <label className="block space-y-1 text-sm"><span className="text-xs text-muted">Idea</span>
      <Textarea rows={3} maxLength={4000} value={idea} onChange={(e) => setIdea(e.target.value)} className="w-full" /></label>
    <div className="grid gap-3 sm:grid-cols-3">{(['in', 'out', 'parked'] as const).map((part) =>
      <label key={part} className="block space-y-1 text-sm">
        <span className="text-xs text-muted">{{ in: 'In scope', out: 'Out of scope', parked: 'Parked for later' }[part]} (one per line)</span>
        <Textarea rows={3} value={scope[part]} onChange={(e) => setScope({ ...scope, [part]: e.target.value })} className="w-full" />
      </label>)}</div>
    <fieldset className="space-y-2"><legend className="text-xs text-muted">Phases</legend>
      {phases.map((phase, index) => <fieldset key={phase.key} className="space-y-2 rounded-md border border-line p-3">
        <legend className="sr-only">Phase {phase.title || phase.key}</legend>
        <div className="flex flex-wrap gap-2">
          <Input aria-label={`Title of phase ${index + 1}`} required maxLength={200} value={phase.title} className="min-w-0 flex-1"
            onChange={(e) => set(index, { title: e.target.value })} />
          <Select aria-label={`Status of phase ${index + 1}`} value={phase.status} onChange={(e) => set(index, { status: e.target.value as PhaseStatus })}>
            {(Object.keys(STATUS_WORDS) as PhaseStatus[]).map((s) => <option key={s} value={s}>{STATUS_WORDS[s]}</option>)}
          </Select>
        </div>
        <Input aria-label={`Summary of phase ${index + 1}`} maxLength={2000} value={phase.summary} className="w-full"
          placeholder="What it delivers" onChange={(e) => set(index, { summary: e.target.value })} />
        <Input aria-label={`How phase ${index + 1} is checked`} maxLength={2000} value={phase.check} className="w-full"
          placeholder="How it will be checked" onChange={(e) => set(index, { check: e.target.value })} />
        {(plan.requirements ?? []).length > 0 && <fieldset className="flex flex-wrap gap-3 text-xs"><legend>Requirements for this phase</legend>
          {(plan.requirements ?? []).map((r) => <label key={r.id} className="flex min-h-6 items-center gap-1.5">
            <input type="checkbox" checked={(phase.requirement_ids ?? []).includes(r.id)} onChange={(e) => set(index, {
              requirement_ids: e.target.checked ? [...(phase.requirement_ids ?? []), r.id] : (phase.requirement_ids ?? []).filter((id) => id !== r.id) })} />
            {r.id}: {r.text}</label>)}
        </fieldset>}
        {phases.length > 1 && <fieldset className="flex flex-wrap gap-x-4 gap-y-1 text-xs"><legend className="mb-1 text-muted">Comes after</legend>
          {phases.filter((other) => other.key !== phase.key).map((other) => <label key={other.key} className="flex min-h-6 items-center gap-1.5">
            <input type="checkbox" checked={phase.depends_on.includes(other.key)} onChange={(e) => set(index, {
              depends_on: e.target.checked ? [...phase.depends_on, other.key] : phase.depends_on.filter((k) => k !== other.key) })} />
            {other.title || other.key}</label>)}
        </fieldset>}
      </fieldset>)}
      <Button size="sm" onClick={addPhase}>Add phase</Button>
      <p className="text-xs text-muted">To remove a phase, set it to Dropped: the plan keeps it in its history.</p>
    </fieldset>
    <label className="block space-y-1 text-sm"><span className="text-xs text-muted">Why (shown in the plan history)</span>
      <Input maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} className="w-full" placeholder="Optional" /></label>
    <div className="flex gap-2">
      <Button type="submit" variant="primary" size="sm" disabled={save.isPending}>Save as a new version</Button>
      <Button size="sm" onClick={() => finish(onCancel)}>{dirty ? 'Discard my edits' : 'Cancel'}</Button>
    </div>
    {save.error && (!(save.error instanceof ApiError && save.error.status === 409) || !latest) ? <ErrorNote error={save.error} /> : null}
  </form>;
}
