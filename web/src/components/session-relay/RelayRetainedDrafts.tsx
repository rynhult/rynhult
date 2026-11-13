import type { RelaySnapshot } from '../../lib/sessionRelayTypes';

export function RelayRetainedDrafts({ inputs, discard }: { inputs: RelaySnapshot['retainedInputs']; discard?: (run: string) => void }) {
  const drafts = Array.isArray(inputs) ? inputs.slice(0, 32).filter(input => input.readOnly === true
    && ['chat', 'terminal'].includes(input.view) && typeof input.sourceRunId === 'string' && typeof input.text === 'string') : [];
  if (!drafts.length) return null;
  return <section aria-label="Previous drafts" className="space-y-2 rounded border border-line p-3">
    <p>Previous drafts stay read-only. They are never sent to the new input.</p>
    {drafts.map(input => <div key={`${input.view}:${input.sourceRunId}`}><label className="block">Previous {input.view === 'chat' ? 'Chat' : 'Terminal'} draft (read-only)
      <textarea aria-label={`Previous ${input.view === 'chat' ? 'Chat' : 'Terminal'} draft (read-only)`} readOnly value={input.text}
        className="block min-h-24 w-full rounded border border-line bg-panel p-2" />
    </label>{discard && input.view === 'chat' && <button type="button" onClick={() => discard(input.sourceRunId)} className="min-h-11 px-3 underline">Discard saved Chat draft</button>}</div>)}
  </section>;
}
