import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Shield } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, post } from '../lib/api';
import { useSingleMutation } from '../lib/mutations';
import { useInstance } from '../lib/queries';
import { useConfirm } from './confirm';
import { Button, Card, CardHeader, ErrorNote, Input, LoadingRows, QueryState } from './ui';

type Item = { id: string; kind: string; state: string; body: string; created: number; result: string; attempts: number; receipt: string; digest: string; destination: string };
type State = { consent: { usage: boolean; reports: boolean; by?: string; at?: string; usage_schema?: string; report_schema?: string }; url: string; total: number; url_locked: boolean; policy: Record<string, string>; items: Item[] };

function Payload({ body, label }: { body: string; label: string }) {
  return <pre role="region" aria-label={label} tabIndex={0} className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-control border border-line bg-panel-2 p-3 font-mono text-xs">{body}</pre>;
}

export function Telemetry() {
  const [offset, setOffset] = useState(0);
  const query = useQuery({ queryKey: ['telemetry', offset], queryFn: () => api<State>(`/api/telemetry?offset=${offset}`), refetchInterval: 15_000 });
  return <div id="telemetry"><Card><CardHeader title="Usage and bug reports" icon={Shield} subtitle="Optional statistics and reviewed reports; both are off by default" />
    <QueryState query={query} loading={<LoadingRows rows={3} />}>
      {data => <Choices data={data} offset={offset} onOffsetChange={setOffset} />}
    </QueryState>
  </Card></div>;
}

/** Telemetry turned off in Admin features: sharing is paused, but deleting data already sent stays available. */
export function TelemetryPaused() {
  const confirm = useConfirm();
  const erase = useSingleMutation({ mutationFn: () => post('/api/telemetry/delete') });
  return <div id="telemetry"><Card><CardHeader title="Usage and bug reports" icon={Shield} subtitle="Turned off in Admin features: nothing new is shared" />
    <div className="space-y-3">
      <p className="text-muted">You can still ask the collector to delete what this installation already sent.</p>
      <Button variant="danger" disabled={erase.isPending} onClick={async () => { if (await confirm({ title: 'Delete data already sent?', body: 'Request deletion for every retained installation key, including previous collector addresses. Anonymous monthly totals cannot be linked back to your installation.', confirm: 'Request deletion', danger: true })) erase.mutate(); }}>Delete data already sent</Button>
      {erase.isSuccess && <p className="text-muted">Deletion queued. Unavailable collectors are retried for 7 days.</p>}
      <ErrorNote error={erase.error} />
      <p className="text-xs text-muted">Deletion can take up to 30 days. Anonymous monthly totals cannot be linked back to your installation or deleted for it.</p>
      <RcFeedback />
    </div>
  </Card></div>;
}

/** On a prerelease, where to report a bug: a public issue, whether or not telemetry is on or in-app reports are offered. */
function RcFeedback() {
  const { version } = useInstance();
  // Match Add machine's prerelease classification: a hyphen before optional +build metadata.
  const prerelease = version?.split('+', 1)[0].includes('-') ?? false;
  if (!prerelease) return null;
  return <section aria-label="RC feedback" className="space-y-2 text-muted">
    <p><a className="text-accent underline underline-offset-2" href="https://github.com/rynhult/rynhult/issues/new" target="_blank" rel="noopener noreferrer">Report an RC bug on GitHub</a>.
      {' '}No telemetry opt-in is needed. If you share bug reports, you can also review and send the in-app report when one is offered.
    </p>
    <p>If you cannot file an issue, contact{' '}
      <a className="text-accent underline underline-offset-2" href="mailto:support@rynhult.dev">support@rynhult.dev</a>.
      {' '}Include version/build, OS/browser, steps, and expected and actual results. Never include secrets or logs containing tokens.</p>
  </section>;
}

function Choices({ data, offset, onOffsetChange }: { data: State; offset: number; onOffsetChange: (offset: number) => void }) {
  const client = useQueryClient();
  const confirm = useConfirm();
  const [usage, setUsage] = useState(data.consent.usage);
  const [reports, setReports] = useState(data.consent.reports);
  const [url, setUrl] = useState(data.url);
  const [deletion, setDeletion] = useState(true);
  const [preview, setPreview] = useState(false);
  useEffect(() => { setUsage(data.consent.usage); setReports(data.consent.reports); setUrl(data.url); }, [data.consent.usage, data.consent.reports, data.url]);
  const refresh = () => Promise.all([
    client.invalidateQueries({ queryKey: ['telemetry'] }),
    client.invalidateQueries({ queryKey: ['telemetry-preview'] }),
  ]);
  const save = useSingleMutation({ mutationFn: () => api('/api/telemetry', { method: 'PUT', json: { usage, reports, url, delete_previous: deletion } }), onSuccess: refresh });
  const erase = useSingleMutation({ mutationFn: () => post('/api/telemetry/delete'), onSuccess: refresh });
  const decision = useSingleMutation({ mutationFn: ({ id, action, digest }: { id: string; action: 'send' | 'discard' | 'edit'; digest?: string }) => post(`/api/telemetry/reports/${id}`, { decision: action, expected_digest: digest, remove_message: action === 'edit' }), onSuccess: refresh });
  const payload = useQuery({ queryKey: ['telemetry-preview'], queryFn: () => api<{ usage: string; reports: string[] }>('/api/telemetry/preview'), enabled: preview });
  const pending = data.items.filter(item => item.kind === 'reports' && item.state === 'queued');
  const history = data.items.filter(item => !pending.includes(item));
  const failed = data.items.filter(item => item.state === 'retry').length;
  const awaiting = data.items.filter(item => item.state === 'approved').length;
  const renderItem = (item: Item) => <details key={item.id} className="border-t border-line pt-3">
      <summary className="cursor-pointer">{item.kind} · {item.state} · {new Date(item.created * 1000).toLocaleString()} · {item.result || 'Not sent'}</summary>
      <div className="space-y-2 py-2"><p className="break-all text-muted">Destination: {item.destination}</p>{item.receipt && <p>Deletion receipt: {item.receipt}</p>}<Payload label={`Payload ${item.id}`} body={item.body} />
        {item.kind === 'reports' && item.state === 'queued' && <div className="flex flex-wrap gap-2">
          <Button disabled={decision.isPending} onClick={() => decision.mutate({ id: item.id, action: 'send', digest: item.digest })}>Send this report</Button>
          <Button disabled={decision.isPending} onClick={() => decision.mutate({ id: item.id, action: 'discard' })}>Don’t send</Button>
          <Button disabled={decision.isPending} onClick={() => decision.mutate({ id: item.id, action: 'edit' })}>Remove message</Button>
        </div>}
      </div>
    </details>;
  return <div className="space-y-4 p-4 text-[13px]">
    <p className="text-muted">The project maintainer operates the default collector. A custom collector has its own data controller. Usage contains only the approved feature, provider and OS fields and bucketed counts. Bug reports are scrubbed, but unknown secrets or names can be missed. Read every report before sending.</p>
    <div className="space-y-3">
      <label className="flex items-center gap-2"><input type="checkbox" checked={usage && !!url && data.policy.usage !== 'off'} disabled={!url || data.policy.usage === 'off'} onChange={e => setUsage(e.target.checked)} />Share usage statistics</label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={reports && !!url && data.policy.reports !== 'off'} disabled={!url || data.policy.reports === 'off'} onChange={e => setReports(e.target.checked)} />Offer bug reports for review</label>
      <p className="text-xs text-muted">One usage summary a day. Each bug report needs your approval and expires after 7 days. Crashed sessions include metadata only. No terminal output or transcripts.</p>
      {data.policy.usage === 'off' && <p className="text-muted">Usage statistics are forced off by installation policy.</p>}
      {data.policy.reports === 'off' && <p className="text-muted">Bug reports are forced off by installation policy.</p>}
      <p className="break-all text-muted">Collector destination (saved): {data.url || 'Not configured'}</p>
      {url !== data.url && <p className="break-all text-muted">Unsaved collector address: {url || 'Not configured'}</p>}
      {!url && <p className="text-xs text-muted">Collection is disabled because no collector address is configured.</p>}
      {data.url_locked && <p className="text-xs text-muted">The collector address is managed by the installation configuration.</p>}
      <label className="flex items-center gap-2"><input type="checkbox" checked={deletion} onChange={e => setDeletion(e.target.checked)} />Also delete previously sent data when turning a choice off</label>
    </div>
    <div className="flex flex-wrap gap-2">
      <Button onClick={() => save.mutate()} disabled={save.isPending}>Save privacy choices</Button>
      <Button onClick={() => { setPreview(true); void client.invalidateQueries({ queryKey: ['telemetry-preview'] }); }}>Show exactly what would be sent</Button>
      <Button variant="danger" disabled={erase.isPending} onClick={async () => { if (await confirm({ title: 'Delete data already sent?', body: 'Request deletion for every retained installation key, including previous collector addresses. Anonymous monthly totals cannot be linked back to your installation.', confirm: 'Request deletion', danger: true })) erase.mutate(); }}>Delete data already sent</Button>
    </div>
    {save.isSuccess && <p className="text-muted">Privacy choices saved.</p>}
    {erase.isSuccess && <p className="text-muted">Deletion queued. The local log shows the result; unavailable collectors are retried for 7 days.</p>}
    <ErrorNote error={save.error ?? erase.error ?? decision.error} />
    <details className="rounded-control border border-line p-3">
      <summary className="cursor-pointer font-medium">Collector and consent details</summary>
      <div className="space-y-3 pt-3">
        <label className="block space-y-1"><span>Collector URL</span><Input type="url" value={url} disabled={data.url_locked} onChange={e => { setUrl(e.target.value); if (!e.target.value) { setUsage(false); setReports(false); } }} className="w-full" spellCheck={false} autoComplete="off" /></label>
        {data.consent.at && <p className="text-xs text-muted">Consent: {data.consent.by}, {new Date(data.consent.at).toLocaleString()}. Schemas: {data.consent.usage_schema}, {data.consent.report_schema}.</p>}
      </div>
    </details>
    {preview && <QueryState query={payload} loading={<LoadingRows rows={2} />}>{value => <Payload label="Usage payload" body={value.usage} />}</QueryState>}
    <p className="text-xs text-muted">Retention: 90 days raw usage, 13 months anonymous monthly totals, 180 days reports; 90 days in this local log. Usage and reports have separate keys, rotated every 90 days. Failed sends stay quiet and local.</p>
    <p className="text-xs text-muted">Deletion can take up to 30 days, including collector backups. Anonymous monthly totals cannot be linked back to your installation or deleted for it.</p>
    {(failed > 0 || awaiting > 0) && <p className="text-muted">Delivery on this page: {failed} sending failed; {awaiting} awaiting delivery. Open History and delivery status for payloads, results and deletion receipts.</p>}
    <section role="group" aria-label="Reports awaiting review" className="space-y-3">
      <h3 className="font-medium">Reports awaiting review ({pending.length} on this page)</h3>
      {!pending.length && <p className="text-muted">No reports awaiting review on this page.</p>}
      {pending.map(renderItem)}
    </section>
    <RcFeedback />
    <details className="rounded-control border border-line p-3">
      <summary className="cursor-pointer font-medium">History and delivery status ({history.length} on this page)</summary>
      <div className="space-y-3 pt-3">
        {!history.length && <p className="text-muted">No other reports or transmissions on this page.</p>}
        {history.map(renderItem)}
      </div>
    </details>
    {(data.total > 50 || offset > 0) && <div className="space-y-2">
      <p className="text-xs text-muted">Showing entries {offset + 1}–{offset + data.items.length} of {data.total}. Older pages can also contain reports awaiting review.</p>
      <div className="flex flex-wrap gap-2">
        <Button disabled={offset === 0} onClick={() => onOffsetChange(Math.max(0, offset - 50))}>Previous log entries</Button>
        <Button disabled={offset + 50 >= data.total} onClick={() => onOffsetChange(offset + 50)}>Older log entries</Button>
      </div>
    </div>}
  </div>;
}
