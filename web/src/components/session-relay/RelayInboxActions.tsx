import { useEffect, useRef, useState } from 'react';
import type { AttentionItem } from '../../lib/types';
import { RelayClient } from '../../lib/sessionRelay';
import { Button, ErrorNote } from '../ui';
import { useOffline } from '../../lib/queries';

/** A fresh authenticated attachment; display facts never become input authority. */
export function RelayInboxActions({ item }: { item: AttentionItem }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [error, setError] = useState<Error>();
  const offline = useOffline();
  const attempted = useRef(false);
  const decision = item.relay_decision!;
  const [expired, setExpired] = useState(decision.expiresAtMs <= Date.now());
  useEffect(() => {
    const timer = setTimeout(() => setExpired(true), Math.max(0, decision.expiresAtMs - Date.now()));
    return () => clearTimeout(timer);
  }, [decision.expiresAtMs]);
  async function answer(choiceId: string) {
    if (attempted.current || busy || result || !item.session_id) return;
    attempted.current = true;
    setBusy(true); setError(undefined);
    const client = new RelayClient(item.session_id);
    try {
      const clientId = crypto.randomUUID();
      const negotiated = await client.call<{ features?: string[] }>('initialize', { major: 1, minor: 0, features: ['decisions'], clientId });
      if (!negotiated.features?.includes('decisions')) throw new Error('Native review is unavailable.');
      await client.call('session.attach', { clientId, connectionSeq: 1 });
      const snapshot = await client.snapshot();
      const current = snapshot.pendingDecisions.find(value => value.decisionId === decision.decisionId);
      if (snapshot.runId !== decision.runId || !current || current.status !== 'pending'
        || current.revision !== decision.revision || current.actionDigest !== decision.actionDigest) throw new Error('This request needs fresh review.');
      // RelayClient registers once and never retries a mutation after a lost reply.
      const outcome = await client.mutate<{ outcome: string }>('decision.reply', snapshot, { decisionId: decision.decisionId, choiceId,
        expectedRevision: decision.revision, actionDigest: decision.actionDigest });
      setResult(outcome.outcome === 'accepted' ? (decision.choices.find(choice => choice.id === choiceId)?.effect === 'deny'
        ? 'Native denial confirmed.' : 'Native allowance confirmed.')
        : 'Answer recorded; the native outcome is unknown. It will not be replayed.');
    } catch (failure) {
      setResult('The answer may have been recorded. Check the session receipt; do not replay it.');
      setError(failure instanceof Error ? failure : new Error('Native review failed.'));
    } finally {
      if (client.attachmentId) await client.call('session.detach', { attachmentId: client.attachmentId }).catch(() => undefined);
      setBusy(false);
    }
  }
  return <div aria-label="Native review actions">
    {decision.status === 'pending' ? decision.choices.map(choice => <Button key={choice.id} size="sm"
      disabled={offline || busy || !!result || !item.relay_decision_available || expired || (!item.relay_allow_enabled && choice.effect !== 'deny')}
      onClick={() => void answer(choice.id)}>{choice.label}</Button>)
      : <p>{decision.status === 'outcome_unknown' ? 'Answer recorded; the native outcome is unknown. It will not be replayed.' : `Native review: ${decision.status}`}</p>}
    {result && <p role="status">{result}</p>}<ErrorNote error={error} />
  </div>;
}
