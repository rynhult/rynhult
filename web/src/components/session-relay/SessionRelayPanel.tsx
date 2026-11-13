import { useNewSession } from '../newSession';
import { useWorkingView } from '../../lib/workingView';
import { useSearchParams } from 'react-router-dom';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { RelayClient, RelayReadReset } from '../../lib/sessionRelay';
import { selectRelayView } from '../../lib/sessionRelaySelector';
import type { RelayEvent, RelayMessage, RelayRetainedInput, RelaySnapshot, RelayView } from '../../lib/sessionRelayTypes';
import { ChatTerminalSelector } from './ChatTerminalSelector';
import { RelayChat } from './RelayChat';
import { RelayDecisions } from './RelayDecisions';
import { RelayRetainedDrafts } from './RelayRetainedDrafts';
import { useRole } from '../../lib/queries';
import { RelayTerminal } from './RelayTerminal';
import { RelayPastRuns } from './RelayPastRuns';
const reasons: Record<string, string> = {
  active_turn: 'The agent is working. Select the view again when it is idle.', background_tool: 'A tool is still running.',
  pending_decision: 'Answer the pending decision first.', login_or_trust_prompt: 'Finish the login or trust prompt first.',
  terminal_draft: 'There is unsent text in Terminal.', queued_send: 'A message is queued.', uncertain_operation: 'An operation has an unknown outcome.',
  recovery_required: 'This session needs explicit recovery before switching.', direction_unqualified: 'This view is read-only; switching is unavailable.',
  not_controller: 'This view is read-only. You do not control this session.', unknown_state: 'The session has not confirmed that it can switch.',
};
function waitsForFirstChat(snapshot?: RelaySnapshot) {
  return !!snapshot && !snapshot.exitOutcome && snapshot.l1?.phase !== 'recovery_required'
    && !!(snapshot.l1?.terminalWaitingForChat || snapshot.l1?.terminalWaitingReason || snapshot.execution?.waitingForFirstChat);
}
export function SessionRelayPanel(props: { sessionId: string; enabled: boolean; projectId?: number | null }) {
  const { username, account, role } = useRole();
  return <Panel key={`${username}:${account}:${role}:${props.sessionId}`} {...props} />;
}
function Panel({ sessionId, enabled, projectId }: { sessionId: string; enabled: boolean; projectId?: number | null }) {
  const [search] = useSearchParams();
  const newSession = useNewSession();
  const requestedTerminal = search.get('tab') === 'terminal';
  useEffect(() => { if (requestedTerminal) setView('terminal'); }, [requestedTerminal]);
  const working = useWorkingView();
  const [pastRun, setPastRun] = useState<string>();
  const [snapshot, setSnapshot] = useState<RelaySnapshot>();
  const [messages, setMessages] = useState<RelayMessage[]>([]);
  const [view, setView] = useState<RelayView>('terminal');
  const waitingForChat = waitsForFirstChat(snapshot);
  useEffect(() => { if (waitingForChat) setView('chat'); }, [waitingForChat]);
  const [attachmentGeneration, setAttachmentGeneration] = useState(0);
  const restoredPreference = useRef(false);
  const [takeReason, setTakeReason] = useState('');
  const [draft, setDraftState] = useState(''); const [error, setError] = useState('');
  const draftRef = useRef(''); draftRef.current = draft;
  const setDraft: typeof setDraftState = value => {
    if (typeof value === 'function') setDraftState(old => { const next = value(old); draftRef.current = next; return next; });
    else { draftRef.current = value; setDraftState(value); }
  };
  const writerGeneration = useRef<string | undefined>(undefined);
  const [retiredDrafts, setRetiredDrafts] = useState<{ generation: string; text: string }[]>([]);
  useLayoutEffect(() => {
    if (!snapshot?.writer && !snapshot?.execution) return;
    const generation = `${snapshot.runId}:${snapshot.writer ? snapshot.controlRevision : 'execution'}`;
    const previous = writerGeneration.current;
    writerGeneration.current = generation;
    if (previous && previous !== generation) {
      const text = draftRef.current;
      if (text) setRetiredDrafts(old => [...old, { generation: previous, text }]);
      draftRef.current = ''; setDraft('');
    }
  }, [snapshot?.runId, snapshot?.controlRevision, snapshot?.writer, snapshot?.execution]);
  const draftValue = draftRef; const previousRun = useRef<string | undefined>(undefined);
  const retired = useRef<RelayRetainedInput[]>([]);
  const [retiredInputs, setRetiredInputs] = useState<RelayRetainedInput[]>([]);
  function retireDraft(run: string) {
    if (!draftValue.current) return;
    if (retired.current.length >= 16) throw new Error('Save or discard a previous Chat draft before switching.');
    retired.current = [...retired.current, Object.freeze({ sourceRunId: run, view: 'chat' as const, text: draftValue.current, readOnly: true as const })];
    setRetiredInputs(retired.current); draftValue.current = ''; setDraft('');
  }
  const [busy, setBusy] = useState(false); const locked = useRef(false);
  const [localUncertain, setLocalUncertain] = useState(false);
  const submittedText = useRef<string | undefined>(undefined);
  const submittedGeneration = useRef<string | undefined>(undefined);
  const [connected, setConnected] = useState(false);
  const [decisionsAvailable, setDecisionsAvailable] = useState(false);
  const [terminalAvailable, setTerminalAvailable] = useState(false);
  const [queueAvailable, setQueueAvailable] = useState(false);
  const [terminalPending, setTerminalPending] = useState(false);
  const [terminalRecovery, setTerminalRecovery] = useState(false);
  const [terminalDirty, setTerminalDirty] = useState(false);
  const [terminalUncertain, setTerminalUncertain] = useState(false);
  const client = useRef(new RelayClient(sessionId));
  const alive = useRef(true);
  useEffect(() => {
    const controller = new AbortController(); alive.current = true;
    const c = new RelayClient(sessionId, controller.signal); client.current = c;
    let timer: ReturnType<typeof setTimeout>; let attachmentId: string | undefined; let cursor: string | undefined;
    const clientId = crypto.randomUUID();
    function receive(events: RelayEvent[]) {
      // Events are invalidations except bounded, complete message records. Unknown events force a new snapshot.
      for (const event of events) {
        if (event.sessionId !== sessionId) throw new Error('Relay event belongs to another session');
        if (event.kind === 'history.reset' || event.kind === 'history.gap') setMessages([]);
        if (event.kind === 'message.upsert') {
          const m = event.payload as unknown as RelayMessage;
          if (typeof m.itemId === 'string' && typeof m.text === 'string' && typeof m.role === 'string') setMessages(old => [...old.filter(x => x.itemId !== m.itemId), m].slice(-200));
        }
      }
    }
    async function poll() {
      try {
        if (!attachmentId) {
          const negotiated = await c.call<{ major: number; features?: string[] }>('initialize', { major: 1, minor: 0, features: ['queue', 'decisions', 'switch', 'terminal', 'writer'], clientId });
          if (negotiated.major !== 1) throw new Error('Unsupported relay version');
          if (!controller.signal.aborted) setTerminalAvailable(Array.isArray(negotiated.features) && negotiated.features.includes('terminal'));
          if (!controller.signal.aborted) setQueueAvailable(Array.isArray(negotiated.features) && negotiated.features.includes('queue'));
          if (!controller.signal.aborted) setDecisionsAvailable(Array.isArray(negotiated.features) && negotiated.features.includes('decisions'));
          const attached = await c.call<{ attachmentId: string; snapshot: RelaySnapshot }>('session.attach', { clientId, connectionSeq: 1 });
          attachmentId = attached.attachmentId;
          const preference = await apiPreference(controller.signal).catch(() => null);
          if (!restoredPreference.current && !controller.signal.aborted) {
            if (requestedTerminal) setView('terminal');
            else if (preference && ['chat', 'terminal'].includes(preference.view)) setView(preference.view);
          }
          restoredPreference.current = true;
        }
        const s = await c.snapshot();
        if (s.execution && s.runId && enabled) {
          try { Object.assign(s, await c.ownershipRead(s.runId)); }
          catch (failure) {
            if (failure instanceof RelayReadReset || failure instanceof ApiError && [401, 403, 404].includes(failure.status)) throw failure;
            s.execution.available = false;
          }
        }
        if (s.sessionId !== sessionId || !Array.isArray(s.receipts) || !Array.isArray(s.pendingDecisions) || !s.capabilities) throw new Error('Relay snapshot is incomplete');
        if (c.pendingOperationId && !s.receipts.some(r => r.operationId === c.pendingOperationId)) s.receipts.push(await c.call('operation.get', { operationId: c.pendingOperationId }));
        s.receipts = await Promise.all(s.receipts.map(r => ['queued', 'submitting', 'uncertain'].includes(r.state) ? c.call<typeof r>('operation.get', { operationId: r.operationId }) : Promise.resolve(r)));
        if (s.history === 'qualified') {
          const history = await c.call<{ items: RelayMessage[] }>('history.page', { limit: 200 });
          if (!controller.signal.aborted) setMessages(history.items);
        }
        const events = await c.call<{ events: RelayEvent[]; cursor?: string }>('events.read', { attachmentId, ...(cursor ? { cursor } : {}), limit: 100 });
        if (controller.signal.aborted) return;
        receive(events.events); cursor = events.cursor;
        const receipt = s.receipts.find(r => r.operationId === c.pendingOperationId);
        if (receipt && ['accepted', 'failed'].includes(receipt.state)) {
          c.pendingOperationId = undefined; setLocalUncertain(false);
          if (receipt.state === 'accepted' && receipt.method === 'turn.submit'
              && writerGeneration.current === submittedGeneration.current) setDraft(old => old === submittedText.current ? '' : old);
        }
        if (s.l1 && previousRun.current && s.runId && s.runId !== previousRun.current) retireDraft(previousRun.current);
        previousRun.current = s.runId;
        if (waitsForFirstChat(s)) setView('chat');
        setSnapshot(s); setConnected(true);
      } catch (e) { if (!controller.signal.aborted) { if (e instanceof RelayReadReset) { attachmentId = undefined; cursor = undefined; setError(''); } else setError((e as Error).message); setConnected(false); if (e instanceof ApiError && [401, 403, 404].includes(e.status)) { setSnapshot(undefined); setMessages([]); setDraft(''); setRetiredDrafts([]); retired.current = []; setRetiredInputs([]); } } }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 2000); }
    }
    void poll();
    return () => {
      alive.current = false; controller.abort(); clearTimeout(timer);
      if (attachmentId) { const detached = new RelayClient(sessionId); detached.connectionToken = c.connectionToken; void detached.call('session.detach', { attachmentId, connectionSeq: 2 }).catch(() => {}); }
    };
  }, [sessionId, attachmentGeneration, requestedTerminal]);
  useEffect(() => { working?.setWork({ dirty: !!draft || terminalDirty, uncertain: localUncertain || terminalUncertain, composing: false }); }, [working, draft, localUncertain, terminalDirty, terminalUncertain]);
  async function act(action: () => Promise<unknown>, allowWhenDisabled = false, refreshSnapshot = true) {
    if (locked.current || !connected || (!enabled && !allowWhenDisabled)) return;
    locked.current = true; setBusy(true); setError('');
    try { await action(); if (alive.current && refreshSnapshot) setSnapshot(await client.current.snapshot()); }
    catch (e) { if (alive.current) { setError(`${(e as Error).message} Check receipts before trying again.`); setLocalUncertain(!!client.current.pendingOperationId || client.current.registrationUncertain); setConnected(false); } }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function select(target: RelayView) {
    if (!snapshot || locked.current || localUncertain) return;
    if (snapshot.exitOutcome || snapshot.l1?.phase === 'recovery_required') { setView(target); return; }
    if (snapshot.nativePrompt) { setView(target); return; }
    if (target === 'terminal' && waitsForFirstChat(snapshot)) return;
    if (snapshot.l1?.available && target !== snapshot.l1.owner && enabled) {
      if (snapshot.l1.phase !== 'owned') { setError('A switch is already in progress. Wait for its outcome.'); return; }
      if (draftValue.current && retired.current.length >= 16) { setError('Save or discard a previous Chat draft before switching.'); return; }
      void act(async () => {
        const plan = await client.current.mutate<{ phase: string; planId: string; planDigest: string; revision: number }>('switch.prepare', snapshot, { target, expectedRevision: snapshot.l1!.revision });
        if (plan.phase !== 'owned') await client.current.mutate('switch.commit', snapshot, { planId: plan.planId, planDigest: plan.planDigest, expectedRevision: plan.revision });
        if (alive.current) { if (snapshot.runId) retireDraft(snapshot.runId); setView(target); }
      });
      return;
    }
    if (snapshot.writer || !enabled || !snapshot.selector) { setView(target); return; }
    const result = selectRelayView({ ...snapshot.selector, target, direction_qualified: snapshot.capabilities[`switch.to_${target}`] === 'qualified' });
    setError(result.reason ? reasons[result.reason] ?? reasons.unknown_state : '');
    if (result.action === 'view_only') { setView(target); return; }
    if (result.action === 'pending') setError('A switch is already in progress. Wait for its outcome.');
    if (result.action !== 'switch') return;
    void act(async () => {
      if (!Number.isSafeInteger(snapshot.ownershipRevision)) throw new Error(reasons.unknown_state);
      const plan = await client.current.mutate<{ planId: string; planDigest: string; revision: number }>('switch.prepare', snapshot, { target, expectedRevision: snapshot.ownershipRevision });
      // The hub verifies the plan revision again. A second client cannot commit a stale plan.
      await client.current.mutate('switch.commit', snapshot, { planId: plan.planId, planDigest: plan.planDigest, expectedRevision: plan.revision });
      if (alive.current) { setView(target); setConnected(false); setAttachmentGeneration(value => value + 1); }
    }, false, false);
  }
  if (!snapshot) return <p role="status">{error || 'Connecting to the shared session…'}</p>;
  const recovering = snapshot.l1?.phase === 'recovery_required' || snapshot.exitOutcome === 'abnormal' || snapshot.exitOutcome === 'unknown';
  const shownView = waitingForChat ? 'chat' : view;
  return <section className="min-w-0 space-y-3" aria-label="Shared session">
    <RelayPastRuns client={client.current} snapshot={snapshot} selected={pastRun} select={setPastRun} connected={connected} />
    <div hidden={!!pastRun}>
    {recovering && <section aria-label="Session recovery" className="space-y-2">
      <p>Recover into a new session. This session's receipts, output and decisions stay readable. Nothing is replayed.</p>
      <button type="button" className="min-h-11 px-3 underline" disabled={busy} onClick={() => newSession({
        initial_prompt: '', ...(projectId ? { mode: 'project' as const, project_id: projectId } : {}), draft_target: `relay-recovery:${sessionId}:${crypto.randomUUID()}`,
      })}>Recover into a new session</button>
      <p>Review and launch the new session explicitly. This session stays here.</p>
    </section>}
    <ChatTerminalSelector terminalWaitingReason={waitingForChat ? snapshot.l1?.terminalWaitingReason : undefined} view={shownView} pending={busy || !connected || localUncertain} waitingForChat={waitingForChat} select={select} />
    <p className="text-xs text-muted">{snapshot.writer ? 'Input belongs to one view at a time.' : snapshot.selector?.shared_live ? 'Chat and Terminal share this live session.' : snapshot.selector ? `Execution is in ${snapshot.selector.owner}. Switching replaces the provider process and keeps the conversation.` : 'The execution interface is unavailable. Retained history and receipts remain readable.'}</p>
    {snapshot.execution && <p role="status" aria-label="Execution readiness">{snapshot.execution.phase !== 'owned'
      ? 'An execution switch is in progress.' : snapshot.selector?.idle_verified
        ? 'Ready to switch views.' : 'Execution is busy or its idle state is unconfirmed.'}</p>}
    {snapshot.writer && <div role="status" aria-label="Input holder">
      <p>{snapshot.writer.active && snapshot.writer.holder
        ? `${snapshot.writer.holder.principal} holds input in ${snapshot.writer.view === 'chat' ? 'Chat' : 'Terminal'}. Other views are read-only.`
        : 'Input is unavailable. Other views are read-only.'}</p>
      {(!snapshot.writer.mine || snapshot.writer.view !== view || (view === 'terminal' && terminalRecovery)) && <><label>Reason for taking input
        <input value={takeReason} onChange={event => setTakeReason(event.target.value)} maxLength={200}
          disabled={!snapshot.writer.available || !enabled || busy || localUncertain || !connected}
          aria-label="Reason for taking input" />
      </label><button type="button" className="min-h-11 px-3 underline"
        disabled={!snapshot.writer.available || !snapshot.writer.active || !enabled || busy || localUncertain || !connected || terminalPending || !takeReason.trim()}
        title={snapshot.writer.available ? 'Take input at idle. The previous draft stays visible and read-only; your new input starts empty. Nothing is replayed.' : 'Input take-over is unavailable for this session.'}
        onClick={() => void act(async () => {
          await client.current.mutate('writer.take', snapshot, { view, expectedRevision: snapshot.controlRevision, reason: takeReason.trim() });
          if (alive.current) setTakeReason('');
        })}>
        Take input in {view === 'chat' ? 'Chat' : 'Terminal'}
      </button></>}
    </div>}
    <RelayDecisions snapshot={snapshot} busy={recovering || busy || !connected || localUncertain} enabled={enabled && !recovering} available={decisionsAvailable}
      answer={(id, choiceId) => {
        const decision = snapshot.pendingDecisions.find(item => item.decisionId === id);
        const choice = decision?.choices.find(item => item.id === choiceId);
        if (decision && choice) void act(() => client.current.mutate('decision.reply', snapshot, {
          decisionId: id, choiceId, expectedRevision: decision.revision, actionDigest: decision.actionDigest }), choice.effect === 'deny');
      }} />
    {snapshot.finalEventsDrained === false && <p role="status">Final output may be incomplete.</p>}
    {snapshot.exitOutcome && <p role="status">{snapshot.exitOutcome === 'normal' ? 'Session ended normally.' : snapshot.exitOutcome === 'abnormal' ? 'Session ended unexpectedly. Explicit recovery is required.' : 'Execution connection ended; native exit outcome is unknown.'}</p>}
    {!enabled && <p role="status">The shared-session preview is off. History and receipts remain readable.</p>}
    {localUncertain && <p role="status">Delivery is unresolved. This page will not send or answer again until a receipt confirms the outcome. If registration was interrupted, use explicit session recovery.</p>}
    {error && <p role="status">{error}</p>}{!connected && <p role="status">Connection unavailable. Messages and answers will not be replayed.</p>}
    {snapshot.nativePrompt && <section role="status" aria-label="Native terminal prompt" className="rounded border border-line p-3">
      <p>{snapshot.nativePrompt.message}</p>
      <a className="inline-flex min-h-11 items-center underline" href={snapshot.nativePrompt.terminalUrl}
        onClick={event => { event.preventDefault(); setView('terminal'); }}>Open Terminal</a>
    </section>}
    <RelayRetainedDrafts inputs={[...(snapshot.retainedInputs ?? []), ...retiredInputs]} discard={run => {
      retired.current = retired.current.filter(input => input.sourceRunId !== run); setRetiredInputs(retired.current);
    }} />
    {retiredDrafts.map(item => <section key={item.generation} aria-label="Previous read-only draft">
      <p>Previous draft — permanently read-only. This text will not be replayed.</p>
      <textarea aria-label="Previous draft" readOnly value={item.text} className="block min-h-24 w-full rounded border border-line bg-panel p-2" />
    </section>)}
    <div hidden={shownView !== 'chat'}><RelayChat snapshot={snapshot} messages={messages} draft={draft} setDraft={setDraft} busy={recovering || busy || !connected || localUncertain || !enabled || !!snapshot.nativePrompt || !!snapshot.writer && (!snapshot.writer.active || !snapshot.writer.mine || snapshot.writer.view !== 'chat') || !!snapshot.execution && (!snapshot.execution.available || !snapshot.execution.mine || snapshot.execution.view !== 'chat')}
      inputReadOnly={recovering || retiredInputs.length >= 16 || !!snapshot.l1 && (snapshot.l1.owner !== 'chat' || snapshot.l1.phase !== 'owned' || busy || !connected || localUncertain || !enabled)}
      send={() => { const text = draft; const generation = writerGeneration.current; submittedText.current = text; submittedGeneration.current = generation; void act(async () => { await client.current.mutate('turn.submit', snapshot, { text, expectedConfigurationRevision: snapshot.configurationRevision, ...(snapshot.writer ? { expectedControlRevision: snapshot.controlRevision } : {}), ifBusy: queueAvailable && snapshot.capabilities.queue === 'qualified' ? 'queue' : 'reject' }); if (alive.current && writerGeneration.current === generation) setDraft(old => old === text ? '' : old); }); }}
      cancel={(operationId, expectedRevision) => void act(() => client.current.call('operation.cancel', { operationId, expectedRevision }))}
      interrupt={() => void act(() => client.current.mutate('turn.interrupt', snapshot, { turnId: snapshot.activeTurnId, expectedControlRevision: snapshot.controlRevision }))} /></div>
    <div hidden={shownView !== 'terminal'}>
      {snapshot.activeTurnId && <button type="button" className="min-h-11 px-3 underline"
        disabled={recovering || busy || !connected || localUncertain || !enabled || snapshot.capabilities.interrupt !== 'qualified'
          || !snapshot.writer?.active || !snapshot.writer.available || !snapshot.writer.mine || snapshot.writer.view !== 'terminal'
          || snapshot.receipts.some(receipt => ['submitting', 'uncertain'].includes(receipt.state))}
        onClick={() => void act(() => client.current.mutate('turn.interrupt', snapshot, {
          turnId: snapshot.activeTurnId, expectedControlRevision: snapshot.controlRevision }))}>Interrupt Terminal turn</button>}
      {snapshot.receipts.some(receipt => receipt.method === 'turn.interrupt') && <ul aria-label="Terminal interrupt receipts">
        {snapshot.receipts.filter(receipt => receipt.method === 'turn.interrupt').map(receipt => <li key={receipt.operationId}>
          {receipt.state === 'accepted' ? 'Interrupt requested.' : receipt.state === 'uncertain' ? 'Interrupt outcome unknown. Check the turn before acting again.' : `Interrupt: ${receipt.state}`}
        </li>)}
      </ul>}
      {!waitingForChat && <RelayTerminal client={client.current} runId={snapshot.runId} input={{ snapshot, enabled: enabled && !recovering, connected, available: terminalAvailable && !recovering,
        busy: busy || localUncertain, progress: state => { setTerminalPending(state.pending); setTerminalRecovery(state.recovery); setTerminalDirty(state.dirty); setTerminalUncertain(state.uncertain); } }} />}
    </div>
    </div>
  </section>;
}
import { api, ApiError } from '../../lib/api';
export function apiPreference(signal?: AbortSignal) {
  return api<{ view: RelayView; revision: number }>('/api/session-relay/preference', { signal });
}
