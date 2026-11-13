import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { knownDecisionReceipts } from '../../lib/sessionRelayDecisionReceipts';
import type { RelaySnapshot } from '../../lib/sessionRelayTypes';

/** Hub-owned decisions stay available in either view; input leases grant no answer authority. */
export function RelayDecisions({ snapshot, busy, enabled, available, answer }: {
  snapshot: RelaySnapshot; busy: boolean; enabled: boolean; available: boolean;
  answer: (id: string, choice: string) => void;
}) {
  // The exact active turn can await this native permission before its final
  // admission receipt. Unrelated unknown operations and previous answers still block.
  const key = JSON.stringify([snapshot.sessionId, snapshot.runId, snapshot.pendingDecisions, snapshot.decisionOutcomes, snapshot.receipts]);
  const [known, setKnown] = useState<{ key: string; ids: Set<string> }>({ key: '', ids: new Set() });
  useEffect(() => {
    let current = true;
    void knownDecisionReceipts(snapshot).then(ids => { if (current) setKnown({ key, ids }); })
      .catch(() => { if (current) setKnown({ key, ids: new Set() }); });
    return () => { current = false; };
  }, [key]); // The key covers every field consumed by the exact binding check.
  const uncertain = snapshot.receipts.some(receipt => ['uncertain', 'submitting'].includes(receipt.state)
    && !(receipt.method === 'decision.reply' && known.key === key && known.ids.has(receipt.operationId))
    && !(receipt.method === 'turn.submit' && receipt.runId === snapshot.runId
      && receipt.operationId === snapshot.execution?.activeOperationId));
  const qualified = available && snapshot.capabilities['decision.tool_approval'] === 'qualified';
  const labels: Record<string, string> = { answered: 'Answered', denied: 'Denied', expired: 'This request expired.',
    invalidated: 'This request needs fresh review.', cancelled: 'This request was cancelled.' };
  const decisions = [...snapshot.pendingDecisions, ...(snapshot.decisionOutcomes ?? [])
    .filter(value => ['answered', 'denied', 'cancelled', 'expired', 'invalidated'].includes(value.status) && !snapshot.pendingDecisions.some(pending => pending.decisionId === value.decisionId))];
  return <>{decisions.map(decision => <section key={decision.decisionId}
    aria-label={['pending', 'reserved', 'outcome_unknown'].includes(decision.status) ? 'Review decision' : 'Decision outcome'} className="rounded border border-line p-3">
    <h3>{['pending', 'reserved', 'outcome_unknown'].includes(decision.status) ? 'Review decision' : 'Decision outcome'}</h3><p className="whitespace-pre-wrap break-words">{decision.description}</p>
    {snapshot.execution && decision.status === 'pending' && <p><Link className="underline" to="/inbox">Review full tool details in Inbox before allowing.</Link></p>}
    {decision.status === 'pending' && <p role="status" className="text-sm text-muted">Only supported basic command approvals can use Enter or Escape in Terminal. File changes and other key combinations are not supported there. Use the review buttons below.</p>}
    {decision.status === 'pending' ? decision.choices.map(choice => <button className="min-h-11 px-3 underline" key={choice.id}
      disabled={busy || uncertain || !qualified || decision.expiresAtMs <= Date.now() || ((!enabled || (!!snapshot.execution || !!snapshot.nativePrompt)) && choice.effect !== 'deny')}
      onClick={() => answer(decision.decisionId, choice.id)}>{choice.label}</button>)
      : <p>{decision.status === 'outcome_unknown' ? 'Answer recorded; the native outcome is unknown. It will not be replayed.'
        : decision.status === 'reserved' ? 'Answer recorded; applying it to the native session.'
        : `${labels[decision.status] ?? 'This request is unavailable.'}${decision.winner ? ` by ${decision.winner.principal}` : ''}`}</p>}
  </section>)}</>;
}
