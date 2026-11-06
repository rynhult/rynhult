import { useEffect, useId, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Play } from 'lucide-react';
import { api } from '../../lib/api';
import { OFFLINE_TITLE, useOffline, useRole } from '../../lib/queries';
import type { SessionSummary } from '../../lib/types';
import { Button, ErrorNote } from '../ui';

type Eligibility = { eligible: boolean; reason: string | null };
const inFlight = new Set<Promise<Eligibility>>();

async function readEligibility(id: string, signal: AbortSignal): Promise<Eligibility> {
  // Only mounted page rows can wait here. A cancelled row never starts a read.
  while (inFlight.size >= 4) {
    signal.throwIfAborted();
    await Promise.race([...inFlight].map(read => read.catch(() => undefined)));
  }
  signal.throwIfAborted();
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(abort, 5000);
  const request = api<Eligibility>(`/api/sessions/${encodeURIComponent(id)}/resume-check`, { signal: controller.signal });
  inFlight.add(request);
  try {
    const result = await request;
    if (typeof result?.eligible !== 'boolean' || (result.eligible ? result.reason !== null
      : typeof result.reason !== 'string' || !result.reason.trim())) throw new Error('Invalid resume eligibility response.');
    return result;
  } finally {
    clearTimeout(timer); signal.removeEventListener('abort', abort); inFlight.delete(request);
  }
}

/** Only the new shell's paged Continue block mounts this read-only hint. POST remains authoritative. */
export function CheckedResume({ session, compact, pending, resume }: {
  session: SessionSummary; compact?: boolean; pending: boolean; resume: () => void;
}) {
  const target = useRef<HTMLDivElement>(null); const [visible, setVisible] = useState(false);
  const reasonId = useId(); const offline = useOffline(); const { username } = useRole();
  useEffect(() => {
    if (!target.current) return;
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)), { rootMargin: '100px' });
    observer.observe(target.current);
    return () => observer.disconnect();
  }, []);
  const check = useQuery({ queryKey: ['resume-check', session.id, username, session.host, session.run_as],
    queryFn: ({ signal }) => readEligibility(session.id, signal),
    enabled: visible && !offline && !session.machine_offline,
    staleTime: 5000, gcTime: 30000, retry: false, refetchInterval: visible ? 10000 : false });
  const reason = session.machine_offline ? 'Machine offline. This action returns when the machine checks in.'
    : offline ? OFFLINE_TITLE : check.isError ? 'Resume eligibility is unavailable. Check again.'
      : !check.data ? 'Checking whether this session can resume…' : check.data.eligible ? null : check.data.reason;
  return <div ref={target} className="flex min-w-0 flex-1 flex-col items-end gap-1">
    <div className="flex flex-wrap justify-end gap-1">
      <Button size={compact ? 'sm' : 'md'} variant="ghost" aria-label="Resume" aria-describedby={reason ? reasonId : undefined}
        title={reason ?? 'Resume the same conversation; the server checks eligibility again.'} disabled={pending || reason !== null}
        onClick={resume}><Play size={14} aria-hidden="true" />{!compact && 'Resume'}</Button>
      {(check.isError || check.data?.eligible === false) && !offline && !session.machine_offline && <Button size="sm" variant="ghost"
        disabled={check.isFetching} onClick={() => void check.refetch()}>Check again</Button>}
    </div>
    {reason && <p id={reasonId} className="w-full break-words text-right text-xs text-muted">{reason}</p>}
    {check.isError && !offline && !session.machine_offline && <ErrorNote error={check.error} />}
  </div>;
}
