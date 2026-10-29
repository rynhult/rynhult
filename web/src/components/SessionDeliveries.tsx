import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { Button, Card, ErrorNote, Input } from './ui';

type Delivery = { id: string; state: string; notice: string | null; preview: string };

/** A 202 is a queue receipt, never proof that the agent received the message. */
export function SessionDeliveries({ id }: { id: string }) {
  const client = useQueryClient();
  const rows = useQuery({ queryKey: ['deliveries', id],
    queryFn: () => api<{ deliveries: Delivery[] }>(`/api/sessions/${id}/deliveries`),
    // Keep pending receipts responsive; settled/empty history is not a live panel.
    refetchInterval: query => query.state.data?.deliveries.some(row =>
      ['queued', 'typing', 'submitted'].includes(row.state)) ? 2000 : 10000,
    refetchIntervalInBackground: false });
  const [text, setText] = useState('');
  const retry = useMutation({ mutationFn: (delivery: Delivery) => post(`/api/deliveries/${delivery.id}/retry`, {}),
    onSuccess: () => client.invalidateQueries({ queryKey: ['deliveries', id] }) });
  const send = useMutation({ mutationFn: () => post(`/api/sessions/${id}/message`, { text }),
    onSuccess: () => { setText(''); void client.invalidateQueries({ queryKey: ['deliveries', id] }); } });
  const pending = rows.data?.deliveries.filter(row => row.state !== 'acknowledged') ?? [];
  if (rows.error) return <ErrorNote error={rows.error} />;
  if (!pending.length) return null;
  return <section aria-label="Message delivery"><Card className="space-y-2 p-3">
    <p className="text-sm font-medium">Message delivery</p>
    <p className="text-xs text-muted">Switch to Activity and close other terminal windows to release terminal control. Queued messages retry automatically; Retry checks again now.</p>
    {pending.map(row => <div key={row.id} className="space-y-1 text-sm">
      <p>{row.preview}</p>
      <p role="status">{row.notice ?? (row.state === 'queued' ? 'Not delivered yet: waiting for the agent to accept input.' : 'Waiting for submission confirmation.')}</p>
      {row.state === 'queued' && <Button size="sm" disabled={retry.isPending} onClick={() => retry.mutate(row)}>Retry delivery</Button>}
    </div>)}
    {pending.some(row => ['failed', 'expired'].includes(row.state)) && <div className="space-y-2">
      <p className="text-xs text-muted">For an unconfirmed delivery, check the terminal first to avoid sending twice. Enter the full message to send it again.</p>
      <Input aria-label="Message to send again" value={text} onChange={event => setText(event.target.value)} />
      <Button disabled={!text.trim() || send.isPending} onClick={() => send.mutate()}>Send again after checking</Button>
    </div>}
    <ErrorNote error={retry.error ?? send.error} />
  </Card></section>;
}
