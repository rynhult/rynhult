import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { post } from '../lib/api';
import { Field, Input, Select } from './ui';

export interface ScheduleConfig {
  timing: { kind: string; time?: string; at?: string; weekday?: number; expression?: string };
  timezone: string;
  missed: string;
  enabled: boolean;
}
export interface ScheduleDraft extends ScheduleConfig { id?: string; launch?: Record<string, unknown> }
export const defaultSchedule = (): ScheduleConfig => ({ timing: { kind: 'daily', time: '09:00' },
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, missed: 'once', enabled: false });

export function ScheduleTiming({ value, onChange }: { value: ScheduleConfig; onChange: (v: ScheduleConfig) => void }) {
  const setTiming = (patch: Partial<ScheduleConfig['timing']>) => onChange({ ...value, timing: { ...value.timing, ...patch } });
  return <div className="space-y-3 rounded-lg border border-line p-3">
    <Field label="Timing"><Select value={value.timing.kind} onChange={e => onChange({ ...value, timing: {
      kind: e.target.value, ...(e.target.value === 'once' ? { at: '' } : e.target.value === 'cron'
        ? { expression: '0 9 * * 1-5' } : { time: '09:00', ...(e.target.value === 'weekly' ? { weekday: 1 } : {}) }) } })}>
      <option value="once">Once</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="cron">Cron expression</option>
    </Select></Field>
    {value.timing.kind === 'once' ? <Field label="Date and time"><Input type="datetime-local" name="schedule-at"
      value={value.timing.at ?? ''} onChange={e => setTiming({ at: e.target.value })} /></Field>
      : value.timing.kind === 'cron' ? <Field label="Cron expression" hint="Five numeric fields: minute hour day month weekday. Lists, ranges and steps; Sunday is 0.">
        <Input name="schedule-cron" spellCheck={false} value={value.timing.expression ?? ''} onChange={e => setTiming({ expression: e.target.value })} /></Field>
      : <Field label="Time"><Input type="time" name="schedule-time" value={value.timing.time ?? '09:00'} onChange={e => setTiming({ time: e.target.value })} /></Field>}
    {value.timing.kind === 'weekly' && <Field label="Weekday"><Select value={value.timing.weekday ?? 1} onChange={e => setTiming({ weekday: Number(e.target.value) })}>
      {['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'].map((day, i) => <option key={day} value={i}>{day}</option>)}
    </Select></Field>}
    <Field label="Timezone" hint="IANA name, such as America/Chicago. Missing DST times are skipped; repeated times run once.">
      <Input name="schedule-timezone" autoComplete="off" spellCheck={false} value={value.timezone} onChange={e => onChange({ ...value, timezone: e.target.value })} />
    </Field>
    <Field label="Missed runs" hint="When Rynhult recovers, never replay a backlog."><Select value={value.missed} onChange={e => onChange({ ...value, missed: e.target.value })}>
      <option value="once">Run once on recovery</option><option value="skip">Skip missed runs</option>
    </Select></Field>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={value.enabled} onChange={e => onChange({ ...value, enabled: e.target.checked })} />Enable schedule</label>
    <NextRun value={value} />
    <p className="text-xs text-muted">Each run checks your budget. If a previous run is still active, the new run is skipped.</p>
  </div>;
}

const when = (iso: string, timeZone?: string) => new Date(iso).toLocaleString(undefined,
  { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', ...(timeZone && { timeZone }) });

/** The next run, as the scheduler will compute it, updated as the timing changes (UX review R5): a schedule that would
    never run, or that is saved paused, says so before it is created. */
function NextRun({ value }: { value: ScheduleConfig }) {
  const key = JSON.stringify([value.timing, value.timezone]);
  const [settled, setSettled] = useState(key);
  useEffect(() => { const timer = window.setTimeout(() => setSettled(key), 400); return () => window.clearTimeout(timer); }, [key]);
  const preview = useQuery({ queryKey: ['schedule-preview', settled], retry: false, staleTime: 30000,
    queryFn: () => { const [timing, timezone] = JSON.parse(settled); return post<{ next: string[] }>('/api/schedules/preview', { timing, timezone }); } });
  const mine = Intl.DateTimeFormat().resolvedOptions().timeZone;
  let text: string, tone = 'text-muted';
  if (preview.error) { text = `Won't run: ${(preview.error as Error).message}`; tone = 'text-wait'; }
  else if (!preview.data || settled !== key) text = 'Next run: working it out…';
  else if (!preview.data.next.length) { text = "Won't run: that time has already passed."; tone = 'text-wait'; }
  else {
    const [first, second] = preview.data.next;
    const there = value.timezone !== mine ? ` (${when(first, value.timezone)} in ${value.timezone})` : '';
    text = `${value.enabled ? 'Next run' : 'Paused. Once enabled, next run'}: ${when(first)} your time${there}${second ? `, then ${when(second)}` : ''}.`;
  }
  return <p className={`text-xs ${tone}`} data-testid="next-run">{text}</p>;
}
