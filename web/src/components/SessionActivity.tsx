import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { AccEvent, SessionDetail } from '../lib/types';
import { providerLabel, useProviderRegistry } from '../lib/queries';
import { stamp } from '../lib/format';
import { eventWords, notificationWords, sourceWords } from '../lib/words';
import { sessionState } from './session-bits';
import { Badge, Card, Empty, QueryState } from './ui';

const stateWords = (raw: string) => sessionState({ observed_state: raw as SessionDetail["observed_state"] }).label;

export function SessionActivity({ id }: { id: string }) {
  const registry = useProviderRegistry();
  const providerName = (provider: string) => providerLabel(provider, registry.byId(provider));
  const events = useQuery({ queryKey: ["events", id], queryFn: () => api<AccEvent[]>(`/api/sessions/${id}/events?limit=300`), refetchInterval: 3000 });
  return (
    <QueryState query={events} isEmpty={(data) => !data.length} empty={<Empty>No events yet.</Empty>}>{(data) => (
    <Card className="overflow-hidden">
      <div className="max-h-full divide-y divide-line overflow-y-auto">
        {[...data].reverse().map((e) => (
          <div key={e.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1 px-4 py-2 text-[13px] sm:grid-cols-[9.5rem_8rem_minmax(0,1fr)] sm:gap-3">
            <span className="font-mono text-[11px] text-faint">{stamp(e.ts)}</span>
            <span className="truncate text-muted" title={e.source}>{sourceWords(e.source, providerName)}</span>
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="font-medium" title={e.kind}>{eventWords(e, stateWords)}</span>
              <span className="block text-xs text-faint sm:ml-2 sm:inline">
                {e.payload.notification_type ? notificationWords(String(e.payload.notification_type))
                  : String(e.payload.reason ?? e.payload.message ?? e.payload.error ?? "")}
              </span>
              {!e.verified && <Badge tone="wait" className="ml-2" title="Not confirmed by the agent's hooks">guessed</Badge>}
            </span>
          </div>
        ))}
      </div>
    </Card>
    )}</QueryState>
  );
}

