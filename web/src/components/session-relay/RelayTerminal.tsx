import { inputOwner } from '../../lib/sessionRelayInputTransport';
import { useEffect, useState } from 'react';
import type { RelayClient } from '../../lib/sessionRelay';
import type { RelaySnapshot } from '../../lib/sessionRelayTypes';
import type { TerminalProgress } from './RelayTerminalInput';
import { RelayTerminalInput } from './RelayTerminalInput';

/** Owned screen reads plus separately person-granted input. No legacy tty fallback. */
export function RelayTerminal({ client, runId, input }: { client: RelayClient; runId?: string | null; input?: {
    snapshot: RelaySnapshot; enabled: boolean; connected: boolean; available: boolean; busy: boolean;
    progress: (state: TerminalProgress) => void } }) {
  const [screen, setScreen] = useState('');
  const readBinding = JSON.stringify([runId, input ? inputOwner(input.snapshot)?.epoch : undefined, client.attachmentId, client.connectionToken]);
  const [readyBinding, setReadyBinding] = useState<string>();
  const screenAvailable = readyBinding === readBinding;
  const [status, setStatus] = useState('Connecting to the shared Terminal…');
  useEffect(() => {
    let alive = true; let timer: ReturnType<typeof setTimeout>;
    setScreen(''); setReadyBinding(undefined);
    async function read() {
      if (!runId) { if (alive) setStatus('The shared Terminal connection is unavailable.'); return; }
      try {
        if (!client.attachmentId) throw new Error('Observer is reconnecting');
        const value = await client.terminalRead(runId);
        if (value.runId !== runId || typeof value.text !== 'string') throw new Error('Terminal read belongs to another run');
        if (alive) { setReadyBinding(readBinding); setScreen(value.text); setStatus(input?.snapshot.exitOutcome && input.snapshot.exitOutcome !== 'unknown' ? 'Retained final Terminal screen.' : value.truncated ? 'Read-only Terminal. Earlier output was truncated.' : 'Read-only Terminal.'); }
      } catch { if (alive) { setReadyBinding(undefined); setScreen(''); setStatus('The shared Terminal connection is unavailable.'); } }
      finally { if (alive) timer = setTimeout(() => void read(), 500); }
    }
    void read();
    return () => { alive = false; clearTimeout(timer); };
  }, [client, runId, readBinding]);
  return <section aria-label="Native Terminal" className="space-y-2">
    <p>{status}</p>
    <pre role="log" aria-label="Native Terminal screen" aria-live="off" tabIndex={0}
      className="max-h-[60vh] overflow-auto whitespace-pre font-mono text-xs">{screen}</pre>
    {input && <RelayTerminalInput client={client} {...input} screenAvailable={screenAvailable} />}
  </section>;
}
