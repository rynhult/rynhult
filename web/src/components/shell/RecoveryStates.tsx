import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ApiError, api } from '../../lib/api';
import { useInstance, useRole } from '../../lib/queries';
import type { BoardView } from '../../lib/types';
import { Badge, Button, Card } from '../ui';

export const unavailableResource = (error: unknown) => error instanceof ApiError && [403, 404].includes(error.status);
const linkClass = 'inline-flex min-h-9 items-center rounded-md border border-line-strong px-3 text-sm text-accent underline underline-offset-2 hover:bg-panel-2';

/** Only owners can read machine inventory. Never substitute session activity for host last-seen. */
export function OfflineHost({ host }: { host?: string | null }) {
  const { owner } = useRole();
  const instance = useInstance();
  const board = useQuery({ queryKey: ['board'], queryFn: () => api<BoardView>('/api/board'), enabled: owner, refetchInterval: 10000 });
  const machine = owner ? board.data?.machines.find(m => m.name === host || (host === 'local' && m.local)) : undefined;
  const seen = machine?.last_seen;
  const date = seen ? new Date(seen) : null;
  return <section aria-label="Offline host" className="mb-4"><Card className="space-y-3 p-5">
    <Badge tone="wait">Offline</Badge>
    <h2 className="text-lg font-semibold">{machine?.label || machine?.name || (host === 'local' ? instance.host : host) || 'This host'} is offline</h2>
    <p className="text-sm text-muted">Your session and location are kept. Resume is unavailable until the machine checks in. Navigation stays available.</p>
    <p className="text-sm text-muted">Last seen: {date && Number.isFinite(date.getTime()) ? <time dateTime={date.toISOString()}>{date.toLocaleString()}</time> : owner && board.isPending ? 'checking machine inventory…' : 'not reported for this view'}</p>
    <p className="text-xs text-muted">Source: {owner ? 'machine inventory; last reported check-in' : 'authorized project or session status; check-in time unavailable'}{owner && board.isError ? ' · inventory could not be refreshed' : ''}.</p>
  </Card></section>;
}

export function UnsupportedChat({ sessionId }: { sessionId: string }) {
  return <Card className="mx-auto max-w-3xl space-y-4 p-6">
    <Badge tone="wait">Unsupported</Badge>
    <h1 className="text-lg font-semibold">Chat isn’t available for this adapter version</h1>
    <p className="text-sm text-muted">The session is available in Terminal. Structured conversation history is not exposed by the installed adapter.</p>
    <dl className="grid grid-cols-2 gap-3 border-y border-line py-4 text-sm"><dt>History</dt><dd>Unsupported</dd><dt>Chat input</dt><dd>Not enabled</dd><dt>In-chat model and effort</dt><dd>Unavailable</dd></dl>
    <Link className={linkClass} to={`/sessions/${encodeURIComponent(sessionId)}`}>Open Terminal</Link>
    <p className="text-xs text-muted">Opens the same session using the existing Terminal controls. This recovery view does not start a session or take input control.</p>
  </Card>;
}

export function UnavailableView({ onRetry, back = '/sessions', detail = 'The resource is unavailable or your access has changed. Its contents and private details are not shown.' }: { onRetry: () => void; back?: string; detail?: string }) {
  return <Card className="mx-auto max-w-3xl space-y-4 p-6">
    <Badge tone="wait">Unavailable</Badge>
    <h1 className="text-lg font-semibold">This saved view can’t be opened</h1>
    <p className="text-sm text-muted">{detail}</p>
    <div className="flex flex-wrap gap-2"><Button onClick={onRetry}>Recheck availability</Button><Link className={linkClass} to={back}>{back === '/projects' ? 'Back to projects' : 'Back to sessions'}</Link></div>
  </Card>;
}


/** Pending Chat's own UI, probe exactly the adapter projection used by P3a.
 * Once Chat is a native session tab, SessionPage leaves this bridge unmounted. */
export function LinkedChatRecovery({ sessionId }: { sessionId: string }) {
  const chat = useQuery({ queryKey: ['chat-recovery', sessionId],
    queryFn: () => api<{ state: string; capabilities: { read_history: boolean } }>(`/api/sessions/${encodeURIComponent(sessionId)}/chat`),
    retry: false, refetchOnWindowFocus: false });
  if (chat.isPending) return <Card className="p-6"><p role="status">Checking Chat support…</p></Card>;
  if (!chat.isError && chat.data?.state === 'unsupported' && !chat.data.capabilities?.read_history) return <UnsupportedChat sessionId={sessionId} />;
  return <UnavailableTab sessionId={sessionId} onRetry={() => { void chat.refetch(); }}
    detail={chat.data?.capabilities?.read_history && !chat.isError
      ? 'Chat history is available, but this build does not include the Chat interface. Open Terminal to continue.'
      : 'Chat availability could not be confirmed. Open Terminal or check again.'} />;
}

export function UnavailableTab({ sessionId, onRetry, detail = 'This tab isn’t available for this session. Open Terminal to choose another view.' }: { sessionId: string; onRetry: () => void; detail?: string }) {
  return <Card className="mx-auto max-w-3xl space-y-4 p-6">
    <Badge tone="wait">Unavailable</Badge>
    <h1 className="text-lg font-semibold">This saved view can’t be opened</h1>
    <p className="text-sm text-muted">{detail}</p>
    <div className="flex flex-wrap gap-2"><Button onClick={onRetry}>Recheck availability</Button><Link className={linkClass} to={`/sessions/${encodeURIComponent(sessionId)}`}>Open Terminal</Link></div>
  </Card>;
}
