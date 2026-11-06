import { useTerminalUpload } from './useTerminalUpload';
import { createPortal } from 'react-dom';
import { FileText, Hand, LogOut, MoreHorizontal } from 'lucide-react';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { useWorkingView, type WorkingViewState } from '../lib/workingView';
import type { TerminalReference } from '../lib/viewRegistry';
import { TerminalTakeDialog } from './TerminalTakeDialog';
import { announce, Button, Dialog, Popover, useMedia } from './ui';
import { useConfirm } from './confirm';

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
  const [output, setOutput] = useState<string | null>(null); const [reader, setReader] = useState(true);
  const [paste, setPaste] = useState<string | null>(null); const [copyMessage, setCopyMessage] = useState('');
  const query = useQuery({ queryKey: ['terminal-reference', sessionId, generation], queryFn: () => api<Omit<TerminalReference, 'installation' | 'person'>>(`/api/sessions/${sessionId}/terminal-reference`), refetchInterval: working.visible && !released ? 2000 : false, staleTime: Infinity, refetchOnWindowFocus: false, retry: false, gcTime: 0 });
  const revision = useSyncExternalStore(owner.subscribe, owner.snapshot);
  useEffect(() => {
    if (!query.data || !slot.current) return;
    try { return owner.acquire(key, { ...query.data, installation: location.origin, person: working.person }, slot.current); }
    catch (failure) { setError((failure as Error).message); }
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
  const write = status?.kind === 'open' && status.write;
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
    <div className="session-terminal-toolbar flex items-center gap-1" title="F6 returns to the view tabs. Closing this view leaves the session running.">
      <span role="status" aria-live="polite" className="text-xs" title="Uploaded paths are relative to the session folder. If you changed directory, return there or adjust the paths.">{upload.message}</span>
      {owner.hiddenPartial(key).slice(0, 1).map(hidden => <span key={hidden} className="inline-flex items-center gap-1 rounded border border-line px-1 text-xs" data-terminal-partial-chip>Unsent text in another tab · <Button size="sm" onClick={() => navigate(`/sessions/${owner.inspect(hidden)?.reference.sessionId}`)}>Show</Button></span>)}
      <strong className="terminal-control-label text-xs" title={controlLabel}>{controlLabel}</strong>
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
      <Button size="sm" title="Open as text" aria-label="Open as text" onClick={asText} disabled={!resource?.terminal}><FileText size={14} aria-hidden="true" /></Button>
      <Popover label="Terminal options" trigger={<MoreHorizontal size={14} aria-hidden="true" />}>{close => <div className="space-y-2 p-1">
        <Button size="sm" onClick={() => { close(); const selection = resource?.terminal?.getSelection(); if (selection) void copy(selection); else { asText(); setCopyMessage('Select text below to copy it.'); } }} disabled={!resource?.terminal}>Copy selected text</Button>
        {status?.kind === 'open' && <>
          {!write && status.held && <Button size="sm" onClick={() => { close(); setTake(true); }}>Take control…</Button>}
          {!write && <Button size="sm" onClick={() => resource?.refreshControl()}>Refresh control status</Button>}
        </>}
        {resource && <Button size="sm" onClick={() => { close(); void disconnect(false); }}>Detach terminal</Button>}
        {!touch && <Button size="sm" disabled={!write} onClick={() => { close(); setPaste(''); }}>Paste text…</Button>}
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={reader} onChange={event => { setReader(event.target.checked); if (resource?.terminal) resource.terminal.options.screenReaderMode = event.target.checked; }} />Screen reader output</label>
        <p className="text-xs text-muted">F6 returns to the view tabs. Closing this view leaves the session running.</p>
      </div>}</Popover>
    </div>
  </>;
  return <div className="min-w-0 space-y-2" data-registered-terminal={sessionId}>
    {toolbarTarget ? createPortal(toolbar, toolbarTarget) : toolbar}
    {(error || query.isError || status?.kind === 'closed' || detached || released) && <div className="space-y-2"><p className="text-xs text-muted">{error || (released ? 'Terminal detached. Recheck to open a new read-only connection.' : detached ? 'Terminal detached after inactivity.' : status?.kind === 'closed' ? status.reason : 'Terminal run unavailable. Recheck after it starts.')}</p><Button size="sm" onClick={() => void disconnect(true)}>Recheck terminal</Button></div>}
    <div ref={slot} className="registered-terminal-slot min-w-0 rounded-md border border-line-input" style={{ height: 'var(--registered-terminal-height, max(320px, 45dvh))' }} />
    {touch && <><div className="flex flex-wrap gap-2" aria-label="Terminal keys">{[['⏎', '\r', 'Enter'], ['Esc', '\x1b', 'Esc'], ['^C', '\x03', '^C (interrupt)'], ['↑', '\x1b[A', 'Up'], ['↓', '\x1b[B', 'Down'], ['Tab', '\t', 'Tab']].map(([label, bytes, name]) => <Button key={name} size="sm" disabled={!write} aria-label={name} onPointerDown={event => event.preventDefault()} onClick={() => resource?.send(bytes)}>{label}</Button>)}</div>
    <details><summary className="cursor-pointer text-xs">More terminal keys</summary><div className="flex flex-wrap gap-2 py-2">{[['←', '\x1b[D'], ['→', '\x1b[C'], ['Page Up', '\x1b[5~'], ['Page Down', '\x1b[6~'], ['Ctrl U (clear line)', '\x15'], ['Ctrl D', '\x04']].map(([label, bytes]) => <Button key={label} size="sm" disabled={!write} onPointerDown={event => event.preventDefault()} onClick={() => resource?.send(bytes)}>{label}</Button>)}<Button size="sm" disabled={!write} onClick={() => setPaste('')}>Paste text…</Button></div></details></>}
    <TerminalTakeDialog open={take} onClose={() => setTake(false)} onTake={reason => resource?.requestControl(true, reason, true)} />
    <Dialog open={output !== null} onClose={() => { setOutput(null); setCopyMessage(''); }} title="Terminal output" footer={<Button onClick={() => void copy(output ?? '')}>Copy output</Button>}><p className="text-xs text-muted">Snapshot of this view's buffered output. Reopen to refresh.</p>{copyMessage && <p role="status">{copyMessage}</p>}<textarea readOnly aria-label="Terminal output" value={output ?? ''} className="h-96 w-full bg-panel font-mono text-xs" /></Dialog>
    <Dialog open={paste !== null} onClose={() => setPaste(null)} title="Paste terminal text" footer={<><Button onClick={() => setPaste(null)}>Cancel</Button><Button disabled={!write || !paste} onClick={() => { resource?.send(paste!); setPaste(null); }}>Paste into terminal</Button></>}><p>Review before sending. Newlines can submit commands or answers.</p><textarea aria-label="Text to paste" value={paste ?? ''} onChange={event => setPaste(event.target.value)} maxLength={60000} className="h-40 w-full bg-panel font-mono text-xs" /></Dialog>
  </div>;
}
