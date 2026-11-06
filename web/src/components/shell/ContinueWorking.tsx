import { OfflineHost } from './RecoveryStates';
import { useState } from 'react';
import { useQueries } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { useRole, useSessions } from '../../lib/queries';
import { groupSessions } from '../../lib/order';
import { useSecurityPolicy } from '../../lib/security';
import type { Project, SessionDetail } from '../../lib/types';
import { Button, Card, CardHeader, Empty, QueryState } from '../ui';
import { SessionList } from '../SessionList';

export function ContinueWorking({ project }: { project: Project }) {
  const sessions = useSessions(); const { owner } = useRole();
  const policy = useSecurityPolicy(owner ? { kind: 'project', project_id: project.id } : undefined);
  const mine = (sessions.data ?? []).filter(s => s.project_id === project.id && s.desired_state !== 'archived');
  const [page, setPage] = useState(0);
  const current = Math.min(page, Math.max(0, Math.ceil(mine.length / 20) - 1));
  const visible = groupSessions(mine).flatMap(g => g.sessions).slice(current * 20, (current + 1) * 20);
  // Members cannot read the owner policy inventory. Reuse each authorized
  // session's existing detail endpoint for its informational older-settings fact.
  const memberDetails = useQueries({ queries: owner ? [] : visible.map(session => ({
    queryKey: ['sessions', session.id], queryFn: () => api<SessionDetail>(`/api/sessions/${session.id}`), refetchInterval: 10000,
  })) });
  const older = owner ? policy.data?.older_sessions : memberDetails.flatMap(q => q.data?.security_older_settings ? [q.data.id] : []);
  return <div className="shell-project-overview"><div className="min-w-0">
    <section aria-label="Sessions" className="min-w-0 space-y-4">{project.machine_offline && <OfflineHost host={project.host} />}<Card><CardHeader title="Sessions" subtitle="Open the same session; Resume and Stop are separate actions." />
      {!owner && memberDetails.some(q => q.isPending) && <p className="p-4 text-sm text-muted">Checking session settings…</p>}
      {!owner && memberDetails.some(q => q.isError) && <p className="p-4 text-sm text-wait">Some session settings are unavailable.</p>}
      <QueryState query={sessions}>{() => mine.length ? groupSessions(visible).map((group, i) => <SessionList key={group.key} sessions={group.sessions} header={i === 0} showProject={false} continueWorking older={older} />) : <Empty>No sessions yet. Start a new session for this project.</Empty>}</QueryState>
      {mine.length > 20 && <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line p-4 text-sm">
        <span>Sessions {current * 20 + 1}–{Math.min((current + 1) * 20, mine.length)} of {mine.length}</span>
        <Button disabled={current === 0} onClick={() => setPage(current - 1)}>Previous sessions</Button>
        <Button disabled={(current + 1) * 20 >= mine.length} onClick={() => setPage(current + 1)}>Next sessions</Button>
      </div>}
    </Card></section>
  </div></div>;
}
