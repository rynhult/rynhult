import { useEffect, useRef, useState } from 'react';
import { MousePointer2 } from 'lucide-react';
import { post } from '../lib/api';
import { readPickerMessage } from '../lib/previewPicker';
import { useOffline, useSessions } from '../lib/queries';
import { useConfirm } from './confirm';
import { announce, Button, ErrorNote } from './ui';

const SEND_HOLD_MS = 800;

/** The app and its inspector share only the isolated preview origin. */
export function PreviewPicker({projectId, port, frameUrl, sessionId}: {projectId: number; port: number; frameUrl: string; sessionId?: string}) {
  const sessions = useSessions();
  const offline = useOffline();
  const confirm = useConfirm();
  const frame = useRef<HTMLIFrameElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const pending = useRef<{id: string; session: string} | null>(null);
  const generation = useRef(0);
  const restoreFocus = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [chosen, setChosen] = useState(sessionId ?? '');
  const [ready, setReady] = useState(false);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const eligible = (sessions.data ?? []).filter(s => s.project_id === projectId && s.management !== 'external'
    && s.desired_state === 'running' && !s.machine_offline && ['active', 'waiting_input', 'idle'].includes(s.observed_state));
  const target = eligible.find(s => s.id === chosen);
  const origin = new URL(frameUrl).origin;
  const send = (type: string, id?: string) => frame.current?.contentWindow?.postMessage(JSON.stringify({v: 1, type, ...(id && {id})}), origin);
  const cancel = () => {
    if (pending.current) send('cancel', pending.current.id);
    pending.current = null;
    clearTimeout(timer.current);
    setArmed(false);
  };
  useEffect(() => {
    if (!armed && !busy && restoreFocus.current) { restoreFocus.current = false; button.current?.focus(); }
  }, [armed, busy]);
  useEffect(() => {
    setBusy(false);
    let alive = true;
    const invalidate = () => {
      const current = pending.current;
      if (current) frame.current?.contentWindow?.postMessage(JSON.stringify({v: 1, type: 'cancel', id: current.id}), origin);
      pending.current = null;
      clearTimeout(timer.current);
      generation.current++;
      setArmed(false);
    };
    const receive = async (event: MessageEvent) => {
      const message = readPickerMessage(event, origin, frame.current?.contentWindow, pending.current?.id ?? null);
      if (!message) return;
      if (message.type === 'ready') { setReady(true); return; }
      const pick = pending.current;
      if (!pick) return;
      restoreFocus.current = true;
      pending.current = null; // one result per person-initiated pick, before any await/dialog
      clearTimeout(timer.current);
      setArmed(false);
      if (message.type !== 'selected') {
        if (message.type === 'unavailable') setError('This page cannot be inspected. Reload the preview and try again.');
        announce(message.type === 'cancelled' ? 'Element selection cancelled.' : 'Element selection unavailable.');
        button.current?.focus();
        return;
      }
      const version = generation.current;
      const current = () => alive && generation.current === version;
      setBusy(true);
      setError(null);
      try {
        const draft = await post<{id: string; text: string}>(`/api/sessions/${pick.session}/preview-element`, {port, ...message.element});
        if (!current()) return;
        // The page decides when this dialog opens, so a double-click or Enter meant for it must not send.
        const ok = await confirm({title: 'Send element to agent?', confirm: 'Send to agent', hold: SEND_HOLD_MS, body: <div className="space-y-3">
          <p>Send this description to <strong className="text-fg">{target?.name}</strong>. Preview content is untrusted; review it before sending.</p>
          <pre role="region" aria-label="Element message" tabIndex={0} className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-panel-2 p-3 font-mono text-xs text-fg">{draft.text}</pre>
        </div>});
        if (!ok) return;
        if (!current()) { announce('Preview changed. Pick the element again before sending.'); return; }
        await post(`/api/sessions/${pick.session}/preview-element/${draft.id}/send`);
        if (current()) announce('Preview element sent to the agent.');
      } catch (cause) {
        if (current()) { setError(cause); announce('Could not send the element. Check the session before picking again.'); }
      } finally {
        if (alive) { restoreFocus.current = true; setBusy(false); }
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && pending.current) { restoreFocus.current = true; invalidate(); button.current?.focus(); announce('Element selection cancelled.'); }
    };
    window.addEventListener('message', receive);
    window.addEventListener('keydown', escape);
    return () => { alive = false; invalidate(); window.removeEventListener('message', receive); window.removeEventListener('keydown', escape); };
  }, [origin, projectId, port, target?.id, offline, confirm]);
  const pick = () => {
    if (!target || !ready || busy || offline) return;
    cancel();
    setError(null);
    const id = crypto.randomUUID();
    pending.current = {id, session: target.id};
    setArmed(true);
    send('pick', id);
    announce('Click a preview element, or focus it and press Enter. Escape cancels.');
    timer.current = setTimeout(() => { restoreFocus.current = true; cancel(); announce('Element selection timed out. Pick again.'); button.current?.focus(); }, 30000);
  };
  const reason = offline ? "Offline: Rynhult isn't answering." : !target ? 'Choose a running Rynhult session for this project.' : !ready ? 'Preview is loading. Reload if it does not become ready.' : undefined;
  return <>
    <div className="space-y-2 border-b border-line px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        {!sessionId && <label className="flex min-w-0 items-center gap-2 text-[13px]">Send to session
          <select aria-label="Send to session" name="preview-session" value={chosen} disabled={armed || busy || offline}
            onChange={event => setChosen(event.target.value)}
            className="h-8 min-w-0 max-w-full rounded-md border border-line-input bg-panel-2 px-2 text-fg focus-visible:ring-2 focus-visible:ring-accent">
            <option value="">{sessions.isPending ? 'Loading sessions…' : 'Choose a running session'}</option>
            {eligible.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>}
        <Button size="sm" disabled={!!reason || armed || busy} title={reason} onClick={event => { button.current = event.currentTarget; pick(); }}>
          <MousePointer2 size={14} aria-hidden="true" />{busy ? 'Preparing or sending…' : 'Pick element'}
        </Button>
        {armed && <Button size="sm" onClick={() => { restoreFocus.current = true; cancel(); button.current?.focus(); announce('Element selection cancelled.'); }}>Cancel picking</Button>}
      </div>
      <p className="text-xs text-muted">{armed ? 'Click an element, or focus it and press Enter. Escape cancels.'
        : sessionId && !target ? 'Start or resume this session to send an element.' : 'Pick an element, then review its description before sending it to the agent.'}</p>
      <ErrorNote error={sessions.error ?? error} />
    </div>
    <iframe ref={frame} title="Project preview" src={frameUrl} onLoad={() => { generation.current++; cancel(); setReady(false); send('hello'); }}
      sandbox="allow-scripts allow-forms allow-same-origin" referrerPolicy="no-referrer"
      className="h-[65vh] min-h-[420px] w-full border-0 bg-panel" />
  </>;
}
