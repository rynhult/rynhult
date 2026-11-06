import { useWorkingView, workingId } from "../lib/workingView";
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react';
import { providerLabel } from '../lib/queries';
import type { SessionDetail } from '../lib/types';
import { Button, Skeleton } from './ui';
import { ChatBoundary } from './ChatSafety';
import { useSessionChat } from './useSessionChat';
const ChatPanel = lazy(() => import('./ChatPanel').then(module => ({ default: module.ChatPanel })));

/** P3a presentation only. Chat never starts a process, claims control or sends input. */
export function SessionChat({ session, tab, terminal, onTerminal, onChat, onAvailability }: {
  session: SessionDetail; tab: string; terminal: ReactNode; onTerminal: () => void; onChat: () => void;
  onAvailability: (id: string, available: boolean) => void;
}) {
  const working = useWorkingView();
  const box = useRef<HTMLDivElement>(null), toggle = useRef<HTMLSpanElement>(null), heading = useRef<HTMLHeadingElement>(null), pane = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0), [split, setSplit] = useState(false), [ratio, setRatio] = useState(60);
  const focusRequested = useRef(false);
  const [visited, setVisited] = useState(tab === 'Terminal'), [read, setRead] = useState(tab === 'Chat');
  const conversation = (tab === 'Terminal' || tab === 'Chat') && session.management !== 'external';
  const wide = width >= 960, beside = !working && tab === 'Terminal' && split && wide;
  const chatVisible = (!working || working.visible) && (tab === 'Chat' || beside);
  const besideRef = useRef(beside); besideRef.current = beside;
  const run = session.runs.reduce<SessionDetail['runs'][number] | undefined>((latest, run) => !latest || run.id > latest.id ? run : latest, undefined);
  const scope = JSON.stringify([session.id, session.native_id, run?.id, !!run?.verified_at, session.run_as, session.host, session.cwd, session.provider]);
  const chat = useSessionChat(session.id, scope, chatVisible);
  const known = useRef({ scope, supported: false });
  if (known.current.scope !== scope) known.current = { scope, supported: false };
  if (chat.page?.capabilities.read_history) known.current.supported = true;
  const available = known.current.supported && chat.page?.state !== 'unsupported';
  const usable = Math.max(1, width - 12), min = Math.min(60, 480 / usable * 100), max = Math.max(min, (usable - 360) / usable * 100);
  const clamped = Math.max(min, Math.min(max, ratio));
  useEffect(() => { onAvailability(session.id, available); }, [session.id, available, onAvailability]);
  useEffect(() => { if (tab === 'Terminal') setVisited(true); if (chatVisible) setRead(true); }, [tab, chatVisible]);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      if (!el.clientWidth) return;
      if (el.clientWidth < 960 && besideRef.current && pane.current?.contains(document.activeElement)) requestAnimationFrame(() => document.getElementById(`${workingId('session-tabs', working)}-tab-Terminal`)?.focus());
      setWidth(el.clientWidth);
    });
    observer.observe(el); return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!chatVisible && pane.current?.contains(document.activeElement)) toggle.current?.querySelector('button')?.focus();
  }, [chatVisible]);
  useEffect(() => {
    if (chatVisible && focusRequested.current && heading.current) { focusRequested.current = false; heading.current.focus(); }
  }, [available, chat.page?.state, tab, chatVisible, onTerminal]);
  const setSize = (value: number) => setRatio(Math.max(min, Math.min(max, value)));
  if (working) return <div data-session-chat-frame className="min-w-0 space-y-3">
    {tab === 'Terminal' && <>
      {available && <Button size="sm" onClick={() => working.openBeside(`/sessions/${session.id}?tab=chat`)}>Open Chat beside terminal</Button>}
      {terminal}
    </>}
    {tab === 'Chat' && <>
      <Button size="sm" onClick={() => working.openBeside(`/sessions/${session.id}`)}>Open terminal beside Chat</Button>
      <Suspense fallback={<Skeleton className="h-full" />}><ChatPanel chat={chat} active={chatVisible} provider={providerLabel(session.provider)} headingRef={node => { heading.current = node; }} onTerminal={onTerminal} /></Suspense>
    </>}
  </div>;
  return <div ref={box} hidden={!conversation} className={conversation ? 'flex h-full min-h-[420px] min-w-0 flex-col gap-2' : 'hidden'}>
    {(available || (tab === 'Chat' && wide)) && <div className="flex shrink-0 flex-wrap items-center gap-2">
      {tab === 'Terminal' && available && <>
        <span ref={toggle}><Button size="sm" disabled={!wide} aria-pressed={beside} aria-expanded={beside} aria-controls={`session-chat-${session.id}`}
          onClick={() => { const opening = !split; focusRequested.current = opening; setSplit(opening); if (!opening) requestAnimationFrame(() => toggle.current?.querySelector('button')?.focus()); }}>
          {beside ? 'Hide Chat beside terminal' : 'Show Chat beside terminal'}</Button></span>
        {!wide && <><span className="text-xs text-muted">Two panes need a wider window.</span><Button size="sm" onClick={onChat}>Open Chat</Button></>}
      </>}
      {tab === 'Chat' && wide && <Button size="sm" onClick={() => { setSplit(true); onTerminal(); }}>Show terminal beside Chat</Button>}
      {beside && <Button size="sm" onClick={() => setRatio(60)}>Reset split</Button>}
    </div>}
    <div className="grid min-h-0 min-w-0 flex-1" style={{ gridTemplateColumns: beside ? `minmax(0,${clamped}fr) 12px minmax(0,${100 - clamped}fr)` : 'minmax(0,1fr)' }}>
      <div hidden={tab !== 'Terminal'} inert={tab !== 'Terminal'} role="region" aria-label="Session terminal" className="min-h-0 min-w-0">
        {conversation && (tab === 'Terminal' || visited) && terminal}
      </div>
      {beside && <div role="separator" aria-label="Terminal and Chat split" aria-orientation="vertical" tabIndex={0}
        aria-valuemin={Math.round(min)} aria-valuemax={Math.round(max)} aria-valuenow={Math.round(clamped)} aria-valuetext={`${Math.round(clamped)} percent terminal width`}
        className="relative cursor-col-resize touch-none rounded-md border-x border-line hover:bg-panel-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        onKeyDown={event => {
          const value = event.key === 'ArrowLeft' ? clamped - 2 : event.key === 'ArrowRight' ? clamped + 2 : event.key === 'Home' ? min : event.key === 'End' ? max : null;
          if (value !== null) { event.preventDefault(); event.stopPropagation(); setSize(value); }
        }} onPointerDown={event => { if (event.button !== 0) return; event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={event => { if (!event.currentTarget.hasPointerCapture(event.pointerId)) return; const rect = box.current!.getBoundingClientRect(); setSize((event.clientX - rect.left) / usable * 100); }}
        onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }} />}
      <div ref={pane} id={`session-chat-${session.id}`} hidden={!chatVisible} inert={!chatVisible} className="min-h-0 min-w-0">
        {read || chatVisible ? <ChatBoundary key={scope} fallback={<section role="region" aria-label="Session chat" className="p-4"><p>Chat could not be displayed. Terminal remains available.</p><Button onClick={onTerminal}>Open Terminal</Button></section>}><Suspense fallback={<Skeleton className="h-full" />}><ChatPanel chat={chat} active={chatVisible} provider={providerLabel(session.provider)} headingRef={node => { heading.current = node; if (node && focusRequested.current) { focusRequested.current = false; node.focus(); } }} onTerminal={onTerminal} /></Suspense></ChatBoundary> : null}
      </div>
    </div>
  </div>;
}
