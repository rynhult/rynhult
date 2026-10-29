import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { CalendarClock } from 'lucide-react';
import { api, post } from '../lib/api';
import { Badge, Button, Card, Empty, ErrorNote, Page, PageHeader, QueryState } from '../components/ui';
import { useConfirm } from '../components/confirm';
import { NewSessionDialog } from '../components/NewSessionDialog';
import type { ScheduleDraft } from '../components/ScheduleTiming';

interface Run { id: string; due_at: string; trigger: string; session_id: string | null; status: string; reason: string | null }
interface Schedule extends ScheduleDraft { id: string; owner: string; launch: Record<string, unknown>; next_run_at: string | null; runs: Run[] }
const date = (value: string | null) => value ? new Date(value).toLocaleString() : 'No further occurrence';
const timing = (row: Schedule) => row.timing.kind === 'cron' ? row.timing.expression
  : row.timing.kind === 'once' ? row.timing.at : `${row.timing.kind}${row.timing.kind === 'weekly' ? ` (${
    ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][row.timing.weekday ?? 0]})` : ''} at ${row.timing.time}`;

export function Schedules() {
  const cache = useQueryClient();
  const confirm = useConfirm();
  const [draft, setDraft] = useState<ScheduleDraft | null>(null);
  const rows = useQuery({ queryKey: ['schedules'], queryFn: () => api<Schedule[]>('/api/schedules'), refetchInterval: 3000 });
  const action = useMutation({
    mutationFn: async ({ row, command }: { row: Schedule; command: string }) => {
      if (command === 'delete' || command === 'cancel') {
        const ok = await confirm({ title: command === 'delete' ? 'Delete schedule' : 'Cancel run',
          body: command === 'delete' ? 'Remove this schedule from the page and stop future runs. Its sessions and audit history remain.'
            : 'Stop this schedule’s active session and cancel queued runs. Future timing stays enabled.',
          confirm: command === 'delete' ? 'Delete schedule' : 'Cancel run', danger: true });
        if (!ok) return;
      }
      return command === 'delete' ? api(`/api/schedules/${row.id}`, { method: 'DELETE' }) : post(`/api/schedules/${row.id}/${command}`);
    },
    onSuccess: () => { cache.invalidateQueries({ queryKey: ['schedules'] }); cache.invalidateQueries({ queryKey: ['sessions'] }); },
  });
  return <Page>
    <PageHeader title="Schedules" description="You choose what runs and when. Every run is an ordinary session under your agent account."
      actions={<Button variant="primary" onClick={() => setDraft({ timing: { kind: 'daily', time: '09:00' },
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, enabled: false, missed: 'once' })}>New schedule</Button>} />
    <ErrorNote error={action.error} />
    <QueryState query={rows} isEmpty={items => !items.length} empty={<Card><Empty icon={CalendarClock}>No schedules yet. Create one to choose a launch and timing.</Empty></Card>}>
      {items => <div className="space-y-4">{items.map(row => <article key={row.id} aria-label={String(row.launch.name)}>
        <Card>
          <div className="flex flex-wrap items-start justify-between gap-3 p-4">
            <div className="min-w-0 space-y-1">
              <h2 className="break-words font-medium">{String(row.launch.name)} <Badge>{row.enabled ? 'Enabled' : 'Paused'}</Badge></h2>
              <p className="break-words text-sm text-muted">{timing(row)} · {row.timezone} · {String(row.launch.provider)} · {row.owner}</p>
              <p className="text-xs text-muted">Next run: <span className="tnum">{row.enabled ? date(row.next_run_at) : 'Paused'}</span> · displayed in your browser’s timezone</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button disabled={action.isPending} onClick={() => action.mutate({ row, command: 'run' })}>Run now</Button>
              <Button disabled={action.isPending} onClick={() => action.mutate({ row, command: row.enabled ? 'disable' : 'enable' })}>{row.enabled ? 'Pause' : 'Enable'}</Button>
              <Button onClick={() => setDraft(row)}>Edit</Button>
              {row.runs.some(run => ['pending', 'claimed', 'running'].includes(run.status)) &&  // only while something is due or running
                <Button disabled={action.isPending} onClick={() => action.mutate({ row, command: 'cancel' })}>Cancel run</Button>}
              <Button variant="danger" disabled={action.isPending} onClick={() => action.mutate({ row, command: 'delete' })}>Delete</Button>
            </div>
          </div>
          <div className="divide-y divide-line border-t border-line">
            {row.runs.length ? row.runs.map(run => <div key={run.id} className="flex flex-wrap items-center gap-2 px-4 py-3 text-xs">
              <span className="tnum text-muted">{date(run.due_at)}</span><Badge tone={run.status === 'failed' ? 'bad' : undefined}>{run.status}</Badge>
              <span className="min-w-0 break-words text-muted">{run.reason ?? (run.trigger === 'person' ? 'Run now' : 'Scheduled run')}</span>
              {run.session_id && <Link className="ml-auto text-accent hover:underline underline underline-offset-2" to={`/sessions/${run.session_id}`}>Open session</Link>}
            </div>) : <p className="px-4 py-3 text-xs text-muted">No runs yet.</p>}
          </div>
        </Card>
      </article>)}</div>}
    </QueryState>
    {draft && <NewSessionDialog open onClose={() => setDraft(null)} schedule={draft} />}
  </Page>;
}
