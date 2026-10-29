import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { api, post } from '../lib/api';
import { useConfirm } from './confirm';
import { Badge, Button, Card, CardHeader, ErrorNote, Field, Input, QueryState } from './ui';

export type UpdateState = {
  version: string; build: string; latest?: string; latest_build?: string; notes?: string; published_at?: string; checked_at?: number;
  available: boolean; repository: string; automatic: boolean; time: string; timezone: string;
  blocked?: string; helper: boolean; pending: boolean; error?: string; session_notice: string;
  progress: { running?: boolean; ok?: boolean; detail?: string; rolled_back?: boolean; rollback_error?: string;
    steps?: { name: string; running?: boolean; ok?: boolean }[] };
};
export const useUpdates = (enabled = true) => useQuery({ queryKey: ['updates'], queryFn: () => api<UpdateState>('/api/updates'), enabled, refetchInterval: 5000 });
const date = (value: string | number) => new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString();

function Controls({ state }: { state: UpdateState }) {
  const client = useQueryClient();
  const confirm = useConfirm();
  const [repository, setRepository] = useState(state.repository);
  const [automatic, setAutomatic] = useState(state.automatic);
  const [time, setTime] = useState(state.time);
  const refresh = () => client.invalidateQueries({ queryKey: ['updates'] });
  const check = useMutation({ mutationFn: () => post('/api/updates/check'), onSuccess: refresh });
  const save = useMutation({ mutationFn: () => api('/api/updates/config', { method: 'PUT', json: { repository, automatic, time } }), onSuccess: refresh });
  const apply = useMutation({ mutationFn: () => post('/api/updates/apply'), onSuccess: refresh });
  const busy = state.pending || state.progress.running || apply.isPending;
  return <div className="space-y-4 p-4 text-[13px]">
    <div className="flex flex-wrap items-center gap-2"><b>Rynhult {state.version}</b><code className="break-all text-xs text-muted">Build {state.build}</code>
      {state.available && <Badge tone="wait">Update available</Badge>}</div>
    {!state.repository && <p className="text-muted">Updates are off. Enter the GitHub owner/repository below and save to turn them on.</p>}
    <p className="text-muted">Last check: {state.checked_at ? date(state.checked_at) : 'Not checked yet'}</p>
    {state.error && <ErrorNote error={state.error} />}
    {state.latest && <div className="space-y-2"><b>Latest: {state.latest}</b>{state.latest_build && <p className="break-all font-mono text-xs text-muted">Build {state.latest_build}</p>}{state.published_at && <p className="text-muted">Published {date(state.published_at)}</p>}
      <p className="whitespace-pre-wrap break-words text-muted">{state.notes || 'No release notes.'}</p></div>}
    <p className="text-muted">{state.session_notice}</p>
    {state.blocked ? <p role="status" className="text-wait">{state.blocked}</p> : !state.helper && <p className="text-muted">Run deploy/install.sh to install the update helper.</p>}
    <div className="flex flex-wrap gap-2">
      <Button disabled={!state.repository || check.isPending || !!busy} onClick={() => check.mutate()}>{check.isPending ? "Checking…" : "Check now"}</Button>
      {!state.blocked && state.available && <Button variant="primary" disabled={!state.helper || !!busy || !!state.error} onClick={async () => {
        if (await confirm({ title: `Update to Rynhult ${state.latest}?`, body: `Rynhult makes a full backup first. The dashboard restarts and terminals disconnect briefly. ${state.session_notice}`, confirm: 'Back up and update' })) apply.mutate();
      }}>Back up and update</Button>}
    </div>
    <div aria-live="polite" className="space-y-1">
      {busy && <p>Update in progress… The dashboard may reconnect while Rynhult restarts.</p>}
      {state.progress.steps?.map((step) => <p key={step.name}>{step.name}: {step.running ? 'Running…' : step.ok ? 'Done' : 'Failed'}</p>)}
      {state.progress.detail && <p>Last result: {state.progress.detail}</p>}
      {state.progress.rolled_back && <p>Previous version restored.</p>}
      {state.progress.rollback_error && <ErrorNote error={state.progress.rollback_error} />}
    </div>
    <div className="space-y-3 border-t border-line pt-4">
      <Field label="GitHub repository" hint="owner/repository; leave empty to turn updates off. Private repositories use data/secrets/update_token on the server; the token is never shown here.">
        <Input value={repository} onChange={e => setRepository(e.target.value)} placeholder="example/acc" autoComplete="off" spellCheck={false} />
      </Field>
      <label className="flex items-center gap-2"><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} />Install updates automatically</label>
      <Field label="Daily time" hint={`Server time (${state.timezone}). Always backs up first; off by default.`}>
        <Input type="time" value={time} onChange={e => setTime(e.target.value)} className="w-36" />
      </Field>
      <Button disabled={save.isPending || !time} onClick={() => save.mutate()}>Save update settings</Button>
      {save.isSuccess && <p role="status">Update settings saved.</p>}
    </div>
    <ErrorNote error={check.error || apply.error || save.error} />
  </div>;
}

export function Updates() {
  const query = useUpdates();
  return <section><Card><CardHeader title="Updates" icon={RefreshCw} subtitle="Back up and update this Rynhult, then sync your other machines" />
    <QueryState query={query}>{state => <Controls state={state} />}</QueryState></Card></section>;
}
