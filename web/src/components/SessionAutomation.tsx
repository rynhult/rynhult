import { useSingleMutation as useMutation } from "../lib/mutations";
import { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { Button, Card, Dialog, ErrorNote, Field, Input, Textarea } from './ui';

export type GoalSettings = { text: string; max_continuations: number; max_hours: number; max_usage?: number | null };
export type Automation = { autoresume?: boolean; goal?: GoalSettings & { used?: number; started?: number; status?: string } | null; waiting?: { reset: number }; notice?: string; dispatch?: { status: string } };
export function AutomationFields({ provider, value, onChange, disabled = false }: { provider: string; value: Automation; onChange: (v: Automation) => void; disabled?: boolean }) {
  const supported = ['claude', 'codex', 'copilot'].includes(provider);
  const goal = value.goal;
  const changeGoal = (patch: Partial<GoalSettings>) => onChange({ ...value, goal: { text: '', max_continuations: 5, max_hours: 2, ...goal, ...patch } });
  return <div className="space-y-3">
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!value.autoresume} disabled={disabled || !supported} onChange={e => onChange({ ...value, autoresume: e.target.checked })} />Resume when the limit resets</label>
    {!supported && <p className="text-xs text-muted">This provider has no reliable plan-limit signal in Rynhult.</p>}
    <Field label="Goal text" hint="Optional. Leave blank to keep goals off."><Textarea disabled={disabled} rows={2} maxLength={4000} value={goal?.text ?? ''} onChange={e => changeGoal({ text: e.target.value })} /></Field>
    {!!goal?.text && <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Maximum continuations"><Input disabled={disabled} type="number" min={1} max={100} value={goal.max_continuations} onChange={e => changeGoal({ max_continuations: Number(e.target.value) })} /></Field>
        <Field label="Maximum hours"><Input disabled={disabled} type="number" min={0.01} max={168} step={0.01} value={goal.max_hours} onChange={e => changeGoal({ max_hours: Number(e.target.value) })} /></Field>
        <Field label="Maximum plan usage (%)" hint="Optional; stops at this window percentage."><Input disabled={disabled} type="number" min={1} max={100} value={goal.max_usage ?? ''} onChange={e => changeGoal({ max_usage: e.target.value ? Number(e.target.value) : null })} /></Field>
      </div>
      <p className="text-xs text-muted">The agent reports GOAL: done or GOAL: blocked: &lt;why&gt;. Typing or a permission prompt stops the goal.</p>
    </>}
  </div>;
}
/** Goal state and editing live in Setup, keeping automation controls off the terminal. */
export function SessionAutomation({ session, disabledReason, editing, onEditing }: {
  disabledReason?: string; session: { id: string; provider: string; automation?: Automation };
  editing: boolean; onEditing: (open: boolean) => void;
}) {
  const [draft, setDraft] = useState<Automation | null>(null);
  const latest = useRef(draft);
  latest.current = draft;
  const query = useQueryClient();
  const state = session.automation ?? {};
  const save = useMutation({
    mutationFn: (value: Automation) => api(`/api/sessions/${session.id}/automation`, { method: 'PUT', json: {
      ...(value.autoresume === undefined ? {} : { autoresume: !!value.autoresume }), ...(value.goal?.text.trim() ? { goal: value.goal } : { goal: null }),
    } }),
    onSuccess: (_, submitted) => {
      setDraft(current => current === submitted ? null : current);
      query.invalidateQueries({ queryKey: ['sessions', session.id] });
    },
  });
  const goal = state.goal;
  const close = () => { setDraft(null); save.reset(); onEditing(false); };
  const shown = state.autoresume || goal || state.waiting || state.notice || (state.dispatch && state.dispatch.status !== 'sent');
  return <>
    <section id="automation" aria-label="Session automation"><Card className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 py-2 text-xs md:px-4 md:py-2.5 md:text-[13px]">
      <h2 className="font-medium text-fg">Goal &amp; auto-resume</h2>
      {!shown && <span className="text-muted">No goal or auto-resume set.</span>}
      {state.autoresume && !state.waiting && <span className="text-muted">Resumes when the plan limit resets</span>}
      {state.waiting && <span role="status" className="text-muted">Waiting for the limit to reset at {new Date(state.waiting.reset * 1000).toLocaleString()}</span>}
      {state.notice && <span role="status" className="text-wait">{state.notice}</span>}
      {state.dispatch && state.dispatch.status !== 'sent' && <span role="status" className="text-muted">Continuation: {state.dispatch.status}</span>}
      {goal && <>
        <span className="text-muted">Goal status: {goal.status}</span><span className="tnum text-muted">{goal.used ?? 0} / {goal.max_continuations} continuations</span>
        {goal.started && <span className="tnum text-muted max-md:hidden">{Math.max(0, (Date.now() / 1000 - goal.started) / 3600).toFixed(2)} / {goal.max_hours} hours</span>}
      </>}
      <span className="ml-auto flex items-center gap-1.5">
        {goal && <Button size="sm" disabled={save.isPending || goal.status === 'stopped'} onClick={() => save.mutate({ goal: null })}>Stop goal</Button>}
        <Button size="sm" variant="ghost" onClick={() => onEditing(true)}>{shown ? "Edit" : "Goal and auto-resume"}</Button>
      </span>
    </Card>{!editing && <ErrorNote error={save.error} />}</section>
    <Dialog open={editing} onClose={close} title="Goal and auto-resume" dismissible={false}
      description="Off unless you turn them on. They belong to this session's person; owners can edit them too."
      footer={<>
        <Button variant="ghost" onClick={close}>Cancel</Button>
        <Button variant="primary" disabled={save.isPending || !draft || !!disabledReason} onClick={() => {
          const value = draft!;
          // A slow save must not discard edits typed meanwhile: close only if nothing changed.
          save.mutate(value, { onSuccess: () => { if (latest.current === value) onEditing(false); } });
        }}>Save</Button>
      </>}>
      <div className="space-y-3">
        {disabledReason && <p className="text-xs text-muted">{disabledReason}</p>}
        <AutomationFields provider={session.provider} value={draft ?? state} onChange={setDraft} disabled={!!disabledReason} />
        <ErrorNote error={save.error} />
      </div>
    </Dialog>
  </>;
}
