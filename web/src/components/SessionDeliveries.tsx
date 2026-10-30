import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { announce, Button, Card, ErrorNote, Input } from './ui';

type Delivery = { id: string; state: string; notice: string | null; preview: string; text?: string; text_truncated?: boolean };
const inFlight = (row: Delivery) => ['queued', 'typing', 'submitted'].includes(row.state);
function useDeliveries(id: string, details = false) {
  return useQuery({ queryKey: details ? ['deliveries', id, 'details'] : ['deliveries', id],
    queryFn: () => api<{ deliveries: Delivery[] }>(`/api/sessions/${id}/deliveries${details ? '?include_text=true' : ''}`),
    refetchInterval: query => query.state.data?.deliveries.some(inFlight) ? 2000 : 10000,
    refetchIntervalInBackground: false });
}
function useRetry(id: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (rows: Delivery[]) => Promise.all(rows.map(row => post(`/api/deliveries/${row.id}/retry`, {}))),
    onSuccess: () => client.invalidateQueries({ queryKey: ['deliveries', id] }) });
}

/** Reuses the phone metadata row; the desktop tab-bar slot stays fixed-height. */
export function SessionDeliveries({ id, onDetails }: { id: string; onDetails: () => void }) {
  const rows = useDeliveries(id);
  const retry = useRetry(id);
  const pending = rows.data?.deliveries.filter(row => row.state !== 'acknowledged') ?? [];
  const active = pending.length > 0;
  const visible = active || !!rows.error;
  const label = `${pending.length} message${pending.length === 1 ? '' : 's'} ${pending.some(inFlight) ? 'waiting to deliver' : 'not delivered'}`;
  const wasVisible = useRef(false);
  useEffect(() => {
    if (visible && !wasVisible.current) announce(rows.error ? 'Message delivery status unavailable. Details in Activity.' : `${label}. Details in Activity.`);
    wasVisible.current = visible;
  }, [visible, label, rows.error]);
  if (!active && !rows.error) return null;
  const queued = pending.filter(row => row.state === 'queued');
  return <section aria-label="Message delivery status" className="session-delivery-indicator pointer-events-auto flex h-full min-w-0 items-center gap-2 text-xs">
    <span className="min-w-0 truncate text-wait" title={retry.error ? String(retry.error) : undefined}>
      <span className="sr-only md:not-sr-only">{rows.error ? 'Delivery status unavailable' : retry.error ? 'Retry failed' : label}</span>
      <span aria-hidden="true" className="md:hidden">{rows.error ? 'Status unavailable' : retry.error ? 'Retry failed' : `${pending.length} ${pending.some(inFlight) ? 'waiting' : 'not delivered'}`}</span>
    </span>
    {queued.length > 0 && <Button size="sm" variant="ghost" disabled={retry.isPending} onClick={() => retry.mutate(queued)}>Retry</Button>}
    <Button size="sm" variant="ghost" onClick={onDetails}>Details</Button>
  </section>;
}

/** Full pending text and retained delivery history belong in Activity, away from the terminal. */
export function DeliveryHistory({ id, focus = false }: { id: string; focus?: boolean }) {
  const client = useQueryClient();
  const rows = useDeliveries(id, true);
  const section = useRef<HTMLElement>(null);
  const shown = !!rows.data?.deliveries.length;
  useEffect(() => { if (focus && shown) section.current?.focus(); }, [focus, shown]);
  const retry = useRetry(id);
  const [text, setText] = useState('');
  const send = useMutation({ mutationFn: () => post(`/api/sessions/${id}/message`, { text }),
    onSuccess: () => { setText(''); void client.invalidateQueries({ queryKey: ['deliveries', id] }); } });
  if (rows.error) return <ErrorNote error={rows.error} title="Couldn't read message delivery history." onRetry={() => rows.refetch()} />;
  if (!rows.data?.deliveries.length) return null;
  const labels: Record<string, string> = { queued: 'Waiting to deliver', typing: 'Typing', submitted: 'Awaiting confirmation',
    acknowledged: 'Delivered', failed: 'Not delivered', expired: 'Expired' };
  return <section ref={section} tabIndex={-1} aria-label="Message delivery history"><Card className="space-y-3 p-3">
    <h2 className="text-sm font-medium">Message delivery</h2>
    <p className="text-xs text-muted">Switch to Activity and close other terminal windows to release terminal control. Queued messages retry automatically; Retry checks again now.</p>
    {rows.data.deliveries.map(row => <div key={row.id} className="space-y-1 border-t border-line pt-2 text-sm">
      <p className="font-medium">{labels[row.state] ?? row.state}</p>
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{row.text ?? row.preview}</p>
      {row.text_truncated && <p className="text-xs text-muted">Saved preview only; the full message is no longer retained.</p>}
      {row.notice && <p className="text-xs text-muted">{row.notice}</p>}
      {row.state === 'queued' && <Button size="sm" disabled={retry.isPending} onClick={() => retry.mutate([row])}>Retry delivery</Button>}
    </div>)}
    {rows.data.deliveries.some(row => ['failed', 'expired'].includes(row.state)) && <div className="space-y-2">
      <p className="text-xs text-muted">For an unconfirmed delivery, check the terminal first to avoid sending twice. Enter the full message to send it again.</p>
      <Input aria-label="Message to send again" value={text} onChange={event => setText(event.target.value)} />
      <Button disabled={!text.trim() || send.isPending} onClick={() => send.mutate()}>Send again after checking</Button>
    </div>}
    <ErrorNote error={retry.error ?? send.error} />
  </Card></section>;
}
