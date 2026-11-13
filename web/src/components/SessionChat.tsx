import { useWorkingView } from "../lib/workingView";
import { lazy, Suspense, useEffect, useRef, type ReactNode } from 'react';
import { providerLabel } from '../lib/queries';
import type { SessionDetail } from '../lib/types';
import { Button, Skeleton } from './ui';
import { ChatBoundary } from './ChatSafety';
import { useSessionChat } from './useSessionChat';
const ChatPanel = lazy(() => import('./ChatPanel').then(module => ({ default: module.ChatPanel })));

/** Chat shares the frame's columns and never starts a process or sends input. */
export function SessionChat({ session, tab, terminal, onTerminal, onChat, onAvailability }: {
  session: SessionDetail; tab: string; terminal: ReactNode; onTerminal: () => void; onChat: () => void;
  onAvailability: (id: string, available: boolean) => void;
}) {
  const working = useWorkingView();
  const chatVisible = (!working || working.visible) && tab === 'Chat';
  const run = session.runs.reduce<SessionDetail['runs'][number] | undefined>((latest, run) => !latest || run.id > latest.id ? run : latest, undefined);
  const scope = JSON.stringify([session.id, session.native_id, run?.id, !!run?.verified_at, session.run_as, session.host, session.cwd, session.provider]);
  const chat = useSessionChat(session.id, scope, chatVisible);
  const known = useRef({ scope, supported: false });
  if (known.current.scope !== scope) known.current = { scope, supported: false };
  if (chat.page?.capabilities.read_history) known.current.supported = true;
  const available = known.current.supported && chat.page?.state !== 'unsupported';
  useEffect(() => { onAvailability(session.id, available); }, [session.id, available, onAvailability]);
  return <div data-session-chat-frame className="min-w-0 space-y-3">
    {tab === 'Terminal' && <>
      {available && <Button size="sm" onClick={() => working ? working.openBeside(`/sessions/${session.id}?tab=chat`) : onChat()}>Open Chat beside terminal</Button>}
      {terminal}
    </>}
    {tab === 'Chat' && <>
      <Button size="sm" onClick={() => working ? working.openBeside(`/sessions/${session.id}`) : onTerminal()}>Open terminal beside Chat</Button>
      <div className="h-[max(360px,60dvh)] min-w-0"><ChatBoundary key={scope} fallback={<section role="region" aria-label="Session chat" className="p-4"><p>Chat could not be displayed. Terminal remains available.</p><Button onClick={onTerminal}>Open Terminal</Button></section>}><Suspense fallback={<Skeleton className="h-full" />}><ChatPanel chat={chat} active={chatVisible} provider={providerLabel(session.provider)} onTerminal={onTerminal} /></Suspense></ChatBoundary></div>
    </>}
  </div>;
}
