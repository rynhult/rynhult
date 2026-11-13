import { useWorkingView } from "../lib/workingView";
import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { announce, Button } from './ui';

type Delivery = { id: string; state: string; notice: string | null; preview: string; text?: string; text_truncated?: boolean };
const inFlight = (row: Delivery) => ['queued', 'typing', 'submitted'].includes(row.state);
export function useDeliveries(id: string, details = false) {
  const view = useWorkingView();
  return useQuery({ queryKey: details ? ['deliveries', id, 'details'] : ['deliveries', id],
    queryFn: () => api<{ deliveries: Delivery[] }>(`/api/sessions/${id}/deliveries${details ? '?include_text=true' : ''}`),
    refetchInterval: query => query.state.data?.deliveries.some(inFlight) ? 2000 : 10000,
    enabled: !view || view.visible, refetchIntervalInBackground: false });
}
export function useRetry(id: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (rows: Delivery[]) => Promise.all(rows.map(row => post(`/api/deliveries/${row.id}/retry`, {}))),
    onSuccess: () => client.invalidateQueries({ queryKey: ['deliveries', id] }) });
}

/** Pending delivery summary; the parent owns its layout, Activity owns full text. */
export function SessionDeliveries({ id, onDetails }: { id: string; onDetails: () => void }) {
  const view = useWorkingView();
  const rows = useDeliveries(id);
  const retry = useRetry(id);
  const pending = rows.data?.deliveries.filter(row => row.state !== 'acknowledged') ?? [];
  const active = pending.length > 0;
  const visible = active || !!rows.error;
  const label = `${pending.length} message${pending.length === 1 ? '' : 's'} ${pending.some(inFlight) ? 'waiting to deliver' : 'not delivered'}`;
  const wasVisible = useRef(false);
  useEffect(() => {
    if ((!view || view.visible) && visible && !wasVisible.current) announce(rows.error ? 'Message delivery status unavailable. Details in Activity.' : `${label}. Details in Activity.`);
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
