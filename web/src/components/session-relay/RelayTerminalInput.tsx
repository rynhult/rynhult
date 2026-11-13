import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { RelayClient } from '../../lib/sessionRelay';
import type { RelayReceipt, RelaySnapshot } from '../../lib/sessionRelayTypes';
import { RelayInputGeneration } from '../../lib/sessionRelayInput';
import { acceptedClaimSupersedes, inputBindingKey, inputOwner, inputTransport } from '../../lib/sessionRelayInputTransport';
export interface TerminalProgress { pending: boolean; recovery: boolean; dirty: boolean; uncertain: boolean }
interface Slot { key: string; generation: RelayInputGeneration; claimReceipt?: RelayReceipt }
export function RelayTerminalInput({ client, snapshot, enabled, connected, available, screenAvailable, busy, progress }: {
  client: RelayClient; snapshot: RelaySnapshot; enabled: boolean; connected: boolean; available: boolean; screenAvailable: boolean; busy: boolean;
  progress: (state: TerminalProgress) => void;
}) {
  const [, update] = useState(0); const [error, setError] = useState('');
  const slots = useRef<Slot[]>([]); const current = useRef<Slot | undefined>(undefined);
  const field = useRef<HTMLTextAreaElement | null>(null); const mounted = useRef(true);
  const focusPending = useRef(false);
  const ownedFocus = useRef(false);
  const key = inputBindingKey(client, snapshot);
  const owner = inputOwner(snapshot);
  const authority = enabled && connected && available && snapshot.capabilities['terminal.input'] === 'qualified'
    && !!owner?.available && owner.active && owner.mine && owner.view === 'terminal';
  const acceptedClaim = current.current?.key === key && current.current.generation.state.phase === 'active'
    ? current.current.claimReceipt : undefined;
  const paused = !!snapshot.nativePrompt || busy || !!snapshot.activeTurnId || snapshot.receipts.some(receipt =>
    ['queued', 'submitting', 'uncertain'].includes(receipt.state) && !acceptedClaimSupersedes(acceptedClaim, receipt))
    || snapshot.pendingDecisions.some(decision => ['pending', 'reserved', 'outcome_unknown'].includes(decision.status));
  const report = useRef(progress); report.current = progress;
  const reported = useRef('');
  function changed() { if (mounted.current) update(value => value + 1); }
  useLayoutEffect(() => {
    const slot = current.current;
    if (slot && (slot.key !== key || !authority || !screenAvailable)) {
      focusPending.current = false; ownedFocus.current = false;
      slot.generation.retire();
      if (slot.key !== key) current.current = undefined;
    }
  }, [key, authority, screenAvailable]);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => { void current.current?.generation.refresh(); }, 500);
    // Disabling an input can blur it to body without a user focus move. Keep
    // that focus intent across temporary pauses, but never take it back from
    // another control the person explicitly focuses or clicks.
    function moved(event: Event) {
      if (event.target === field.current || event.type === 'focusin' && event.target === document.body) return;
      focusPending.current = false; ownedFocus.current = false;
    }
    document.addEventListener('focusin', moved, true);
    document.addEventListener('pointerdown', moved, true);
    return () => {
      mounted.current = false; clearInterval(timer);
      document.removeEventListener('focusin', moved, true);
      document.removeEventListener('pointerdown', moved, true);
      for (const slot of slots.current) slot.generation.retire();
    };
  }, []);
  const generation = current.current?.generation;
  const ready = authority && screenAvailable && !paused && !generation?.state.waiting;
  useLayoutEffect(() => {
    if (!ready && ownedFocus.current) focusPending.current = true;
    if (!focusPending.current || generation?.state.phase !== 'active' || !ready) return;
    // Claim completion schedules a render. Focus only after the active field
    // exists and is enabled; respect focus moved elsewhere during the await.
    if (document.activeElement === document.body || document.activeElement === field.current) {
      field.current?.focus();
      if (document.activeElement === field.current) focusPending.current = false;
    }
  }, [generation, generation?.state.phase, ready]);
  useEffect(() => {
    const state = { pending: !!generation && (generation.state.phase === 'claiming' || generation.state.phase === 'active' && (generation.state.pending > 0 || generation.state.waiting)),
      recovery: !!generation && ['retired', 'uncertain'].includes(generation.state.phase),
      dirty: slots.current.some(slot => !!slot.generation.state.draft), uncertain: slots.current.some(slot => slot.generation.state.uncertain) };
    const value = JSON.stringify(state);
    if (value !== reported.current) { reported.current = value; report.current(state); }
  });
  async function connect() {
    if (!authority || !screenAvailable || paused || current.current) return;
    if (slots.current.length >= 16) { setError('Retained input capacity reached. Review these drafts before starting another session.'); return; }
    let slot: Slot;
    const transport = inputTransport(client, snapshot, receipt => { slot.claimReceipt = receipt; });
    slot = { key, generation: new RelayInputGeneration(owner!.epoch, transport, changed) };
    slots.current.push(slot); current.current = slot; focusPending.current = true; setError(''); changed();
    await slot.generation.connect();
  }
  function typing(event: KeyboardEvent<HTMLTextAreaElement>, slot: Slot) {
    if (event.ctrlKey || event.metaKey || event.altKey) return; // browser selection/copy only; paste is separately refused
    event.preventDefault();
    if (!authority || paused || !screenAvailable || current.current !== slot) return;
    if (event.nativeEvent.isComposing) { setError('Composition input is unavailable here. Use Chat for this text.'); return; }
    const data = event.key === 'Enter' ? '\r' : event.key === 'Backspace' ? '\x7f' : event.key;
    try { slot.generation.enqueue(data); setError(''); } catch (failure) { setError((failure as Error).message); }
  }
  return <section aria-label="Terminal keyboard" className="space-y-2">
    {authority && !current.current && <button type="button" className="min-h-11 rounded border border-line px-3" disabled={!screenAvailable || paused}
      onClick={() => void connect().catch(() => setError('Input is unavailable. Nothing will be replayed.'))}>Connect Terminal input</button>}
    {slots.current.map((slot, index) => {
      const live = slot === current.current && slot.generation.state.phase === 'active';
      const claiming = slot === current.current && slot.generation.state.phase === 'claiming';
      return <section key={index} aria-label={live ? 'Current Terminal input' : 'Retained Terminal input'}>
        <p>{slot.generation.state.status}</p>
        <label>{live ? 'Terminal typing (append at end)' : claiming ? 'Connecting Terminal input' : 'Previous Terminal draft — permanently read-only'}
          <textarea ref={live ? field : undefined} aria-label={live ? 'Terminal typing' : claiming ? 'Connecting Terminal input' : 'Previous Terminal draft'} readOnly disabled={live && (paused || slot.generation.state.waiting)} value={slot.generation.state.draft}
            onFocus={live ? () => { ownedFocus.current = true; } : undefined}
            onKeyDown={live ? event => typing(event, slot) : undefined} onBeforeInput={event => event.preventDefault()}
            onPaste={event => { event.preventDefault(); setError('Paste is unavailable in this input profile. Use Chat for this text.'); }}
            onCompositionStart={event => { event.preventDefault(); setError('Composition input is unavailable here. Use Chat for this text.'); }}
            className="block min-h-20 w-full rounded border border-line bg-panel p-2" />
        </label>
      </section>;
    })}
    {generation?.state.phase === 'active' && <p className="text-xs text-muted">Basic typing, Enter and Backspace only. Typing appends at the end; paste and other native controls are unavailable.</p>}
    {error && <p role="status">{error}</p>}
  </section>;
}
