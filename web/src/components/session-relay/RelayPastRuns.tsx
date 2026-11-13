import { useEffect, useRef, useState } from 'react';
import type { RelayClient } from '../../lib/sessionRelay';
import type { RelayMessage, RelaySnapshot } from '../../lib/sessionRelayTypes';
import { RelayTerminal } from './RelayTerminal';

/** Read-only retained selection; never calls register, take, launch or input. */
export function RelayPastRuns({ client, snapshot, selected, select, connected }: {
  client: RelayClient; snapshot: RelaySnapshot; selected?: string; select: (value?: string) => void; connected: boolean;
}) {
  const generation = useRef(0);
  const [runs, setRuns] = useState<{ runId: string; exitOutcome: string }[]>([]);
  const [listMore, setListMore] = useState(false);
  const [messages, setMessages] = useState<RelayMessage[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const binding = JSON.stringify([client.connectionToken, client.attachmentId, snapshot.runId, snapshot.controlRevision, snapshot.historyEpoch, connected]);
  useEffect(() => {
    let alive = true;
    if (!connected) return;
    void client.retainedRuns().then(value => { if (alive) { setRuns(value.runs); setListMore(value.more); } }).catch(() => {
      if (alive) { setRuns([]); setError('Retained runs are unavailable.'); }
    });
    return () => { alive = false; };
  }, [client, binding, connected]);
  useEffect(() => {
    let alive = true;
    generation.current++;
    setBusy(false);
    setMessages([]); setCursor(undefined); setMore(false); setError('');
    if (!selected || !connected) return;
    setBusy(true);
    void client.retainedEvents(selected).then(value => {
      if (!alive) return;
      setMessages(value.events.filter(event => event.kind === 'message.upsert').map(event => event.payload as unknown as RelayMessage)
        .filter(message => typeof message.itemId === 'string' && typeof message.text === 'string' && typeof message.role === 'string').slice(-200));
      setCursor(value.cursor); setMore(value.more);
    }).catch(() => { if (alive) setError('Retained history is unavailable.'); }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; generation.current++; };
  }, [client, selected, binding, connected]);
  async function loadMore() {
    if (!selected || !cursor || busy || !connected) return;
    const run = selected, epoch = generation.current;
    setBusy(true);
    try {
      const value = await client.retainedEvents(run, cursor);
      if (epoch !== generation.current) return;
      const incoming = value.events.filter(event => event.kind === 'message.upsert').map(event => event.payload as unknown as RelayMessage)
        .filter(message => typeof message.itemId === 'string' && typeof message.text === 'string' && typeof message.role === 'string');
      setMessages(old => [...old.filter(message => !incoming.some(item => item.itemId === message.itemId)), ...incoming].slice(-200));
      setCursor(value.cursor); setMore(value.more);
    } catch { if (epoch === generation.current) { setMessages([]); setError('Retained history is unavailable.'); } }
    finally { if (epoch === generation.current) setBusy(false); }
  }
  if (!runs.length && !selected) return null;
  return <section aria-label="Retained runs" className="space-y-2">
    <label>Retained run<select aria-label="Retained run" value={selected ?? ''} disabled={!connected || busy}
      onChange={event => select(event.target.value || undefined)}>
      <option value="">Current session</option>
      {selected && !runs.some(run => run.runId === selected) && <option value={selected}>Run {selected} — unavailable</option>}
      {runs.map(run => <option key={run.runId} value={run.runId}>Run {run.runId} — {run.exitOutcome}</option>)}
    </select></label>
    {listMore && <p>Only the 50 most recent retained runs are listed.</p>}
    {selected && <section aria-label="Past run history" className="space-y-2">
      <p>Run {selected} — read-only. Selecting history does not take input or replay a message.</p>
      {error && <p role="status">{error}</p>}
      <div role="log" aria-label="Past run conversation" className="max-h-[40dvh] overflow-auto whitespace-pre-wrap break-words">
        {messages.map(message => <p key={message.itemId}><strong>{message.role}: </strong>{message.text}</p>)}
      </div>
      <p>Only retained messages from this run are shown; at most 200 loaded messages are displayed.</p>
      {more && <button type="button" disabled={busy || !connected} onClick={() => void loadMore()}>Load more retained history</button>}
      <RelayTerminal key={`${selected}:${binding}`} client={client} runId={selected} />
    </section>}
  </section>;
}
