import type { RelayMessage, RelaySnapshot } from '../../lib/sessionRelayTypes';
export function RelayChat({ snapshot, messages, draft, setDraft, busy, send, cancel, interrupt, inputReadOnly = false }: {
  snapshot: RelaySnapshot; messages: RelayMessage[]; draft: string; setDraft: (value: string) => void; busy: boolean;
  send: () => void; cancel: (id: string, revision: number) => void; interrupt: () => void;
  inputReadOnly?: boolean;
}) {
  const uncertain = snapshot.receipts.some(r => r.state === 'uncertain' || r.state === 'submitting');
  return <div className="space-y-3">
    <div role="log" aria-label="Conversation" className="max-h-[55dvh] overflow-auto whitespace-pre-wrap break-words">{messages.map(m => <p key={m.itemId}><strong>{m.role}: </strong>{m.text}</p>)}</div>
    {snapshot.history !== 'qualified' && <p>Only retained messages are shown; full conversation history is unavailable.</p>}
    <ul aria-label="Operation receipts">{snapshot.receipts.map(r => <li key={r.operationId}>{r.method}: {r.state === 'uncertain' ? 'Delivery unknown. Check the conversation before sending again.' : r.method === 'turn.interrupt' && r.state === 'accepted' ? 'Interrupt requested.' : r.state}{r.reason ? ` · ${r.reason}` : ''}{r.state === 'queued' && <button className="min-h-11 px-3 underline" disabled={busy} onClick={() => cancel(r.operationId, r.revision)}>Cancel queued message</button>}</li>)}</ul>
    <form onSubmit={e => { e.preventDefault(); send(); }} className="space-y-2">
      <label className="block">Message<textarea key={`${snapshot.runId}:${snapshot.writer ? snapshot.controlRevision : 'shared'}`} aria-label="Message" maxLength={snapshot.l1 ? 8192 : undefined} readOnly={inputReadOnly || !!snapshot.writer && (!snapshot.writer.active || !snapshot.writer.mine || snapshot.writer.view !== 'chat') || !!snapshot.execution && (!snapshot.execution.mine || snapshot.execution.phase !== 'owned' || snapshot.execution.view !== 'chat')}
        className="block min-h-24 w-full rounded border border-line bg-panel p-2" value={draft} onChange={e => setDraft(e.target.value)} /></label>
      <button className="min-h-11 rounded border border-line px-3" disabled={busy || uncertain || !draft.trim() || snapshot.capabilities.text !== 'qualified' || !snapshot.runId}>Send{snapshot.activeTurnId ? ' to queue' : ''}</button>
      {snapshot.activeTurnId && <button className="min-h-11 px-3 underline" type="button" disabled={busy || snapshot.capabilities.interrupt !== 'qualified' || (snapshot.writer ? !snapshot.writer.mine || snapshot.writer.view !== 'chat' : !snapshot.execution?.mine || snapshot.execution.view !== 'chat' || snapshot.execution.phase !== 'owned' || snapshot.execution.activeOperationId !== snapshot.activeTurnId)} onClick={interrupt}>Interrupt</button>}
    </form>
  </div>;
}
