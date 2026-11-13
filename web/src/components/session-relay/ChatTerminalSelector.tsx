import { useId } from 'react';
import type { RelayView } from '../../lib/sessionRelayTypes';
export function ChatTerminalSelector({ view, pending, select, waitingForChat = false, terminalWaitingReason }: { view: RelayView; pending: boolean; waitingForChat?: boolean; terminalWaitingReason?: string; select: (view: RelayView) => void }) {
  const explanation = useId();
  const waiting = waitingForChat || !!terminalWaitingReason;
  const reason = terminalWaitingReason ?? 'Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.';
  return <div role="group" aria-label="Session view" className="flex gap-2">
    {(['chat', 'terminal'] as const).map(v => <button key={v} type="button" className="min-h-11 rounded border border-line px-3 text-sm" aria-pressed={view === v} disabled={pending || v === 'terminal' && waiting} aria-describedby={v === 'terminal' && waiting ? explanation : undefined} onClick={() => select(v)}>{v === 'chat' ? 'Chat' : 'Terminal'}</button>)}
    {waiting && <p id={explanation} role="status">{reason}</p>}
  </div>;
}
