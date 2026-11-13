import { useRole } from '../../lib/queries';
import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import type { RelayView } from '../../lib/sessionRelayTypes';
export function RelayPreference() {
  const { username, role, account } = useRole();
  return <Preference key={`${username}:${role}:${account}`} />;
}
function Preference() {
  const [preference, setPreference] = useState<{ view: RelayView; revision: number }>();
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => { const c = new AbortController(); void api<{ view: RelayView; revision: number }>('/api/session-relay/preference', { signal: c.signal }).then(setPreference).catch(e => { if (!c.signal.aborted) setError(e.message); }); return () => c.abort(); }, []);
  return <label className="block">Open shared sessions in<select className="ml-2 min-h-11 rounded border border-line bg-panel" disabled={!preference || busy} value={preference?.view ?? 'terminal'} onChange={e => {
    setBusy(true); setError(''); void api<{ view: RelayView; revision: number }>('/api/session-relay/preference', { method: 'PUT', json: { view: e.target.value, expectedRevision: preference?.revision } }).then(setPreference).catch(e => setError(e.message)).finally(() => setBusy(false));
  }}><option value="chat">Chat first</option><option value="terminal">Terminal first</option></select>{error && <span role="status">{error}</span>}</label>;
}
