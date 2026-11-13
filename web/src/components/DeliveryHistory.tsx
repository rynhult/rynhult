import { useWorkingView } from '../lib/workingView';
import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { post } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { Button, Card, ErrorNote, Input } from './ui';
import { useDeliveries, useRetry } from './SessionDeliveries';

/** Full pending text and retained delivery history belong in Activity, away from the terminal. */
export function DeliveryHistory({ id, focus = false }: { id: string; focus?: boolean }) {
  const view = useWorkingView();
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
    <p className="text-xs text-muted">{view ? "Use Release control in the terminal or its hidden-controller notice." : "Switch to Activity and close other terminal windows to release terminal control."} Queued messages retry automatically; Retry checks again now.</p>
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
