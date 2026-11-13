import { lazyImport } from '../lib/lazyImport';
import { useTerminalUpload } from './useTerminalUpload';
import { createPortal } from 'react-dom';
import { FileText, Hand, LogOut, MoreHorizontal } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { useWorkingView, type WorkingViewState } from '../lib/workingView';
import type { TerminalReference } from '../lib/viewRegistry';
import { announce, Button, Popover, useMedia } from './ui';
import { useConfirm } from './confirm';

const TerminalPasteDialog = lazy(() => lazyImport(() => import('./TerminalPasteDialog')));
const TerminalPhoneKeys = lazy(() => lazyImport(() => import('./TerminalPhoneKeys')));
const RegisteredExpansion = lazy(() => lazyImport(() => import('./RegisteredExpansion')));
const TerminalTakeDialog = lazy(() => lazyImport(() => import('./TerminalTakeDialog')).then(module => ({ default: module.TerminalTakeDialog })));
const TerminalText = lazy(() => lazyImport(() => import('./TerminalText')).then(module => ({ default: module.TerminalText })));

export function RegisteredTerminal({ sessionId, toolbarTarget }: { sessionId: string; toolbarTarget?: HTMLElement | null }) {
  const working = useWorkingView();
  return working ? <TerminalView sessionId={sessionId} working={working} toolbarTarget={toolbarTarget} /> : <Link to={`/sessions/${sessionId}`}>Open terminal view</Link>;
}
function TerminalView({ sessionId, working, toolbarTarget }: { sessionId: string; working: WorkingViewState; toolbarTarget?: HTMLElement | null }) {
  const navigate = useNavigate();
  const touch = useMedia('(pointer: coarse)');
  const owner = working.terminals; const key = working.key;
  const confirm = useConfirm(); const [released, setReleased] = useState(false);
  const slot = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(''); const [generation, setGeneration] = useState(0);
  const [take, setTake] = useState(false);
  const [output, setOutput] = useState<string | null>(null); const [reader, setReader] = useState(() => { try { return localStorage.getItem('acc-terminal-screen-reader') !== 'off'; } catch { return true; } });
  const [paste, setPaste] = useState<string | null>(null); const [copyMessage, setCopyMessage] = useState('');
  const query = useQuery({ queryKey: ['terminal-reference', sessionId, generation], queryFn: () => api<Omit<TerminalReference, 'installation' | 'person'>>(`/api/sessions/${sessionId}/terminal-reference`), refetchInterval: working.visible && !released ? 2000 : false, staleTime: Infinity, refetchOnWindowFocus: false, retry: false, gcTime: 0 });
  const revision = useSyncExternalStore(owner.subscribe, owner.snapshot);
  useEffect(() => {
    if (!query.data || !slot.current) return;
    let cancelled = false;
    let dispose: (() => void) | undefined;
    void owner.prepare().then(() => {
      if (!cancelled && slot.current) dispose = owner.acquire(key, { ...query.data!, installation: location.origin, person: working.person }, slot.current);
    }).catch(failure => { if (!cancelled) setError((failure as Error).message); });
    return () => { cancelled = true; dispose?.(); };
  }, [owner, key, query.data, working.person]);
  const [documentVisible, setDocumentVisible] = useState(() => !document.hidden);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const changed = () => {
      clearTimeout(timer);
      if (document.hidden) timer = setTimeout(() => setDocumentVisible(false), 1500);
      else setDocumentVisible(true);
    };
    document.addEventListener('visibilitychange', changed);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', changed); };
  }, []);
  const resource = owner.resource(key); const status = resource?.status;
  const visible = working.visible && documentVisible;
  useEffect(() => { if (query.data && !error) try { owner.setVisibility(key, visible); } catch (failure) { setError((failure as Error).message); } }, [owner, key, query.data, visible, error, revision]);
  const releasedWhenHidden = useRef(false);
  useEffect(() => {
    if (!visible && resource?.writerToken && !resource.pendingInput && !resource.busyInput) {
      releasedWhenHidden.current = true; resource.releaseControl();
    }
  }, [resource, visible, revision]);
  useEffect(() => {
    if (!resource || resource !== owner.resource(key) || !working.explicit || !working.intent || !visible || document.hidden || status?.kind !== 'open') return;
    // Page-owned intent survives remounts without replaying the claim.
    if (owner.consumeIntent(key, `${working.intent}:${query.data?.runId}`) || releasedWhenHidden.current) {
      // The server marks the person's own unsent text with reclaim; someone else's still needs the dialog.
      if (!status.write && !status.held && (!status.partial || status.reclaim)) { releasedWhenHidden.current = false; resource.requestControl(); }
    }
  }, [resource, working.explicit, working.intent, visible, status, generation, owner, key, query.data?.runId]);
  const focusedIntent = useRef<string | undefined>(undefined);
  useEffect(() => {
    // Do not defer a pending focus request across a browser-tab return.
    const suppress = () => { if (document.hidden) focusedIntent.current = working.intent; };
    suppress(); document.addEventListener('visibilitychange', suppress);
    return () => document.removeEventListener('visibilitychange', suppress);
  }, [working.intent]);
  useEffect(() => {
    if (!working.explicit || !working.intent || focusedIntent.current === working.intent || !resource?.terminal || status?.kind !== 'open' || !visible) return;
    // Consume even when suppressed: returning from another browser tab must not focus it later.
    focusedIntent.current = working.intent;
    if (!touch && !window.matchMedia('(max-width: 767px)').matches && !document.hidden
      && !document.querySelector('[role="dialog"], dialog[open]')) resource.terminal.focus();
  }, [working.explicit, working.intent, resource, status?.kind, visible, touch]);
  useEffect(() => {
    if (!resource?.terminal) return;
    resource.setScreenReader(reader);
    resource.host.querySelector('.live-region')?.setAttribute('aria-live', 'off');
  }, [resource, resource?.terminal, reader]);
  const write = status?.kind === 'open' && status.write;
  const [expanded, setExpanded] = useState(false);
  const expansionRoot = useRef<HTMLDivElement>(null);
  const collapse = useCallback(() => setExpanded(false), []);
  useLayoutEffect(() => {
    if (!expanded) return;
    const shortcut = (event: KeyboardEvent) => {
      if (event.isComposing || [...document.querySelectorAll<HTMLElement>('[role="menu"], [role="dialog"], [role="alertdialog"]')].some(dialog => dialog !== expansionRoot.current && dialog.getClientRects().length && getComputedStyle(dialog).visibility !== 'hidden')) return;
      if (event.key === 'F6') {
        event.preventDefault(); event.stopImmediatePropagation();
        expansionRoot.current?.querySelector<HTMLButtonElement>('[data-collapse-terminal]')?.focus();
      } else if (event.key.toLowerCase() === 'c' && !event.ctrlKey && !event.metaKey && !event.altKey
        && event.target instanceof HTMLElement && event.target.closest('.session-terminal-toolbar')) {
        event.preventDefault(); event.stopImmediatePropagation(); collapse();
      }
    };
    window.addEventListener('keydown', shortcut, true);
    return () => window.removeEventListener('keydown', shortcut, true);
  }, [expanded, collapse]);
  const wasExpanded = useRef(false);
  useEffect(() => {
    if (wasExpanded.current && !expanded) document.querySelector<HTMLButtonElement>(`[data-session-expand="${CSS.escape(sessionId)}"]`)?.focus({ preventScroll: true });
    wasExpanded.current = expanded;
  }, [expanded]);
  useEffect(() => { if (!visible && expanded) setExpanded(false); }, [visible, expanded]);
  const upload = useTerminalUpload(sessionId, () => ({ socket: resource?.socket, write: !!write, insert: text => resource?.send(text) }), resource?.host);
  const controlLabel = write ? 'You control this terminal' : status?.kind === 'connecting' || query.isPending ? 'Connecting…' : 'Read-only terminal';
  const attachment = owner.inspect(key);
  const detached = attachment?.state === 'detached' && !attachment.waiting;
  useEffect(() => { if (status?.kind === 'closed') { setOutput(null); setPaste(null); } }, [status?.kind]);
  useEffect(() => { if (status?.kind === 'open') announce(write ? 'Terminal control acquired.' : 'Terminal is read-only.'); }, [status?.kind, write]);
  useEffect(() => {
    if (!resource) return;
    const leave = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === 'c' && !event.altKey && !event.isComposing && (event.metaKey || event.ctrlKey && event.shiftKey) && resource.terminal?.hasSelection()) {
        event.preventDefault(); event.stopPropagation(); void copy(resource.terminal.getSelection()); return;
      }
      if (event.key !== 'F6' || event.isComposing) return;
      event.preventDefault(); event.stopPropagation();
      document.querySelector<HTMLButtonElement>(`[data-view-tab="${CSS.escape(key)}"]`)?.focus();
    };
    resource.host.addEventListener('keydown', leave, true);
    return () => resource.host.removeEventListener('keydown', leave, true);
  }, [resource, key]);
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopyMessage(''); announce('Terminal text copied.'); }
    catch { setOutput(text); setCopyMessage('Clipboard access was denied. Select and copy the text below.'); }
  }
  const disconnect = async (reconnect: boolean) => {
    if (resource?.composing) { setError('Finish composing before detaching this terminal.'); return; }
    const pending = !!resource?.pendingInput;
    if (pending && !await confirm({ title: 'Detach with unresolved input?', body: 'Some input may already have reached the session. Detaching cannot recall it. Reconnecting starts read-only and never replays input. Check the terminal before typing again.', confirm: 'Detach without replay', danger: true })) return;
    try { owner.release(key, false, pending); setError(''); setReleased(!reconnect); if (reconnect) setGeneration(value => value + 1); }
    catch (failure) { setError((failure as Error).message); }
  };
  function asText() {
    const buffer = resource?.terminal?.buffer.active;
    let text = '';
    if (buffer) for (let index = 0; index < buffer.length; index++) {
      const line = buffer.getLine(index); text += (index && !line?.isWrapped ? '\n' : '') + (line?.translateToString(!buffer.getLine(index + 1)?.isWrapped) ?? '');
    }
    setOutput(text || 'No buffered output.');
  }
  const toolbar = <>
    <div className="session-terminal-toolbar flex items-center gap-1" title={expanded ? "F6 focuses the bar; C collapses. Escape goes to the terminal." : "F6 returns to the view tabs. Closing this view leaves the session running."}>
      <span role="status" aria-live="polite" className="text-xs" title="Uploaded paths are relative to the session folder. If you changed directory, return there or adjust the paths.">{upload.message}</span>
      {owner.hiddenPartial(key).slice(0, 1).map(hidden => <span key={hidden} className="inline-flex items-center gap-1 rounded border border-line px-1 text-xs" data-terminal-partial-chip>Unsent text in another tab · <Button size="sm" onClick={() => navigate(`/sessions/${owner.inspect(hidden)?.reference.sessionId}`)}>Show</Button></span>)}
      <strong className="terminal-control-label text-xs" title={controlLabel}>{controlLabel}</strong>
      {write && resource?.partialInput && <span data-terminal-notice title="Partial input may remain. Inspect it before typing." className="max-w-12 truncate text-xs text-wait">Partial input</span>}
      {status?.kind === 'open' && <>
        {!write && status.held && <span className="inline-flex items-center gap-1 rounded border border-line px-1 text-xs" data-terminal-holder-chip>Another controller has control · <Button size="sm" title="Take over" aria-label="Control terminal" onClick={() => setTake(true)}><Hand size={14} aria-hidden="true" /></Button></span>}
        {!write && !status.held && status.partial && !status.reclaim && <span className="text-xs text-muted" data-terminal-unsent-chip title="Text typed or pasted earlier was never sent. Take control to inspect it.">Unsent input waiting</span>}
        {!write && !status.held && <Button size="sm" title="Control terminal" aria-label="Control terminal" disabled={write} onClick={async () => {
          if (status.held) { setTake(true); return; }
          if (status.partial && !status.reclaim && !await confirm({ title: 'Control with partial input?', body: 'Partial input may remain in the terminal. Inspect it before typing. It will not be cleared or replayed.', confirm: 'Control without clearing' })) return;
          resource?.requestControl(false, '', true);
        }}><Hand size={14} aria-hidden="true" /></Button>}
        {write && <Button size="sm" title="Release control" aria-label="Release control" onClick={async () => {
          if (resource?.busyInput) { setError('Finish pending input or composition before releasing control.'); return; }
          if (resource?.partialInput && !await confirm({ title: 'Release with partial input?', body: 'Partial input may remain in the terminal. Releasing does not clear or replay it.', confirm: 'Release without clearing' })) return;
          setError(''); resource?.releaseControl(true);
        }}><LogOut size={14} aria-hidden="true" /></Button>}
      </>}
      <Button data-session-expand={sessionId} size="sm" data-collapse-terminal={expanded || undefined} aria-label={expanded ? 'Collapse terminal' : 'Expand terminal'} onClick={() => setExpanded(!expanded)}>{expanded ? 'Collapse' : 'Expand'}</Button>
      <Button size="sm" title="Open as text" aria-label="Open as text" onClick={asText} disabled={!resource?.terminal}><FileText size={14} aria-hidden="true" /></Button>
      <Popover label="Terminal options" trigger={<MoreHorizontal size={14} aria-hidden="true" />}>{close => <div className="space-y-2 p-1">
        <Button size="sm" onClick={() => { close(); const selection = resource?.terminal?.getSelection(); if (selection) void copy(selection); else { asText(); setCopyMessage('Select text below to copy it.'); } }} disabled={!resource?.terminal}>Copy selected text</Button>
        {write && resource?.partialInput && <span data-terminal-notice title="Partial input may remain. Inspect it before typing." className="max-w-12 truncate text-xs text-wait">Partial input</span>}
      {status?.kind === 'open' && <>
          {!write && status.held && <Button size="sm" onClick={() => { close(); setTake(true); }}>Take control…</Button>}
          {!write && <Button size="sm" onClick={() => resource?.refreshControl()}>Refresh control status</Button>}
        </>}
        {resource && <Button size="sm" onClick={() => { close(); void disconnect(false); }}>Detach terminal</Button>}
        {!touch && <Button size="sm" disabled={!write} onClick={() => { close(); setPaste(''); }}>Paste text…</Button>}
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={reader} onChange={event => { setReader(event.target.checked); try { localStorage.setItem('acc-terminal-screen-reader', event.target.checked ? 'on' : 'off'); } catch { announce('Screen reader preference could not be saved in this browser.'); } }} />Screen reader output</label>
        <p className="text-xs text-muted">F6 returns to the view tabs. Closing this view leaves the session running.</p>
      </div>}</Popover>
    </div>
  </>;
  return <div ref={expansionRoot} role={expanded ? 'dialog' : undefined} aria-modal={expanded || undefined} aria-owns={expanded ? resource?.host.id : undefined} aria-label={expanded ? 'Expanded terminal' : undefined}
    style={expanded ? { position: 'fixed', inset: 0, height: '100dvh', zIndex: 40, background: 'var(--panel, #0b0d10)', padding: 12, display: 'flex', flexDirection: 'column' } : undefined}
    className="min-w-0 space-y-2" data-registered-terminal={sessionId}>
    {!expanded && toolbarTarget ? createPortal(toolbar, toolbarTarget) : toolbar}
    {(error || query.isError || status?.kind === 'closed' || detached || released) && <div className="space-y-2"><p className="text-xs text-muted">{error || (released ? 'Terminal detached. Recheck to open a new read-only connection.' : detached ? 'Terminal detached after inactivity.' : status?.kind === 'closed' ? status.reason : 'Terminal run unavailable. Recheck after it starts.')}</p><Button size="sm" onClick={() => void disconnect(true)}>Recheck terminal</Button></div>}
    <div ref={slot} className="registered-terminal-slot min-w-0 rounded-md border border-line-input" style={{ height: expanded ? undefined : 'var(--registered-terminal-height, max(320px, 45dvh))', flex: expanded ? 1 : undefined, minHeight: expanded ? 0 : undefined }} />
    {touch && <Suspense><TerminalPhoneKeys resource={resource} write={!!write} visible={visible} paste={() => setPaste('')} /></Suspense>}
    {expanded && expansionRoot.current && <Suspense><RegisteredExpansion element={expansionRoot.current} host={resource?.host.parentElement} /></Suspense>}
    {take && <Suspense><TerminalTakeDialog open onClose={() => setTake(false)} onTake={reason => resource?.requestControl(true, reason, true)} /></Suspense>}
    {output !== null && <Suspense><TerminalText text={output} message={copyMessage} refresh={asText} copy={() => void copy(output)} close={() => { setOutput(null); setCopyMessage(''); }} /></Suspense>}
    {paste !== null && <Suspense><TerminalPasteDialog value={paste} change={setPaste} write={!!write} send={text => resource?.send(text)} /></Suspense>}
  </div>;
}
