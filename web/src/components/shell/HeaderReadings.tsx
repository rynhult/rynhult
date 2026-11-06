import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../lib/api';
import { ago } from '../../lib/format';
import type { AccountHealth, BoardView, Observation } from '../../lib/types';
import { useInstance, useRole } from '../../lib/queries';
import { Popover, QueryState } from '../ui';

/** Owner readings retain their existing authority; members issue none of these queries. */
type Readings = { machine_load: ReactNode; plan_usage: ReactNode };
export function HeaderReadings({ children }: { children: (readings: Readings) => ReactNode }) {
  return useRole().owner ? <OwnerReadings>{children}</OwnerReadings> : children({ machine_load: null, plan_usage: null });
}
function OwnerReadings({ children }: { children: (readings: Readings) => ReactNode }) {
  const instance = useInstance();
  const board = useQuery({ queryKey: ['board'], queryFn: () => api<BoardView>('/api/board'), refetchInterval: 10000 });
  const usage = useQuery({ queryKey: ['providers'], queryFn: () => api<{ observations: Record<string, Omit<Observation, 'key'>> }>('/api/providers'), refetchInterval: 60000 });
  const accounts = useQuery({ queryKey: ['accounts', 'local', ''], queryFn: () => api<AccountHealth>('/api/accounts'), refetchInterval: 60000 });
  // Queries live above presentation: hiding a reading or closing overflow never changes their authority.
  return children({
    machine_load: board.data === undefined && board.error ? <Link className="text-wait" to="/host" title="Machine load unavailable. Open Host for details.">Load unavailable</Link> : <QueryState query={board} loading={<span>Machine load: checking…</span>}>{value => {
      const machine = value.machines.find(m => m.local); const light = machine?.light;
      return <Link to="/host" title={`Machine load · ${machine?.label ?? instance.host} · ${light ? ago(new Date(light.collected_at * 1000).toISOString()) : 'no reading'}`}>
        {machine?.connected === false ? 'Machine offline' : 'CPU'} {light?.cpu_percent == null ? 'unavailable' : `${Math.round(light.cpu_percent)}%`} · Memory {light?.memory_percent == null ? 'unavailable' : `${Math.round(light.memory_percent)}%`}{machine?.stale && ' · stale'}
      </Link>;
    }}</QueryState>,
    plan_usage: usage.data === undefined && usage.error ? <Link className="text-wait" to="/providers" title="Plan usage unavailable. Open Providers for details.">Usage unavailable</Link> : <QueryState query={usage} loading={<span>Plan usage: checking…</span>}>{value => {
      const rows: { label: string; window: string; percent: number; observation: Omit<Observation, 'key'> }[] = [];
      for (const [key, window] of [['five_hour', '5-hour'], ['seven_day', '7-day']]) {
        const o = value.observations['claude.rate_limits']; const pct = o?.value[key]?.used_percentage;
        if (typeof pct === 'number') rows.push({ label: 'Claude', window, percent: pct, observation: o });
      }
      for (const key of ['primary', 'secondary']) {
        const o = value.observations['codex.rate_limits']; const w = o?.value[key];
        if (typeof w?.used_percent === 'number') rows.push({ label: 'Codex', window: w.window_minutes ? `${w.window_minutes / 60}-hour` : key, percent: w.used_percent, observation: o });
      }
      const summary = [...new Set(rows.map(row => row.label))].map(label => `${label} ${Math.round(Math.max(...rows.filter(row => row.label === label).map(row => row.percent)))}%`).join(' · ');
      return rows.length ? <Popover label={`${summary} · Plan usage details`} trigger={<span title={rows.map(row => `${row.label} ${row.window}: ${Math.round(row.percent)}% · ${ago(new Date(row.observation.observed_at * 1000).toISOString())} · ${row.observation.source}`).join('; ')}>{summary}</span>} wide>
        {close => <div className="space-y-2"><p>Plan usage · {accounts.data?.default_account ?? 'account unavailable'}@{instance.host}</p><p className="text-xs text-muted">Highest used window for each provider. Readings are account totals.</p>
          <ul>{rows.map(row => <li key={row.label + row.window}><Link className="block py-2 underline" to="/providers" onClick={close}>{row.label} · {row.window} · {Math.round(row.percent)}% · {ago(new Date(row.observation.observed_at * 1000).toISOString())} · {row.observation.source}</Link></li>)}</ul></div>}
      </Popover> : <Link to="/providers">Plan usage: no readings</Link>;
    }}</QueryState>,
  });
}
