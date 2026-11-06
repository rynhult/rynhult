import { useEffect, useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { useWorkingView } from '../../lib/workingView';
import type { Project, SessionDetail } from '../../lib/types';
import { Button } from '../ui';
import { UnavailableView } from './RecoveryStates';

/** Restored references are untrusted. Access loss is latched until an explicit recheck. */
export function ViewAccess({ children, onAccess, allowArchived = false }: { allowArchived?: boolean; children: ReactNode; onAccess: (available: boolean | undefined) => void }) {
  const working = useWorkingView()!; const cache = useQueryClient();
  const url = new URL(working.href, location.origin);
  const projectId = /^\/projects\/(\d+)/.exec(url.pathname)?.[1];
  const sessionId = /^\/sessions\/([^/]+)/.exec(url.pathname)?.[1] ?? url.searchParams.get('session');
  const [denied, setDenied] = useState(false);
  const queryKey = projectId ? ['projects'] : ['sessions', sessionId];
  const query = useQuery<Project[] | SessionDetail>({ queryKey,
    queryFn: () => projectId ? api<Project[]>('/api/projects') : api<SessionDetail>(`/api/sessions/${sessionId}`),
    enabled: !denied, retry: false });
  const missing = !!query.data && (Array.isArray(query.data) ? !query.data.some(item => String(item.id) === projectId) : !allowArchived && query.data.observed_state === 'archived');
  useEffect(() => { if (allowArchived && query.data && !Array.isArray(query.data) && query.data.observed_state === 'archived') setDenied(false); }, [allowArchived, query.data]);
  const revoked = missing || query.error instanceof ApiError && [401, 403, 404].includes(query.error.status);
  useEffect(() => {
    if (revoked) {
      setDenied(true); working.documents.close(working.key);
      if (sessionId) working.terminals.revokeSession(sessionId);
      const prefixes = projectId ? [`/api/files/project/${projectId}`] : [`/api/files/session/${sessionId}`, `/api/files/workspace/${sessionId}`];
      cache.removeQueries({ predicate: item => prefixes.includes(String(item.queryKey[1])) || item.queryKey[0] === 'sessions' && item.queryKey[1] === sessionId });
    }
    onAccess(denied || revoked ? false : query.isSuccess ? true : undefined);
  }, [query.isSuccess, denied, revoked, working.documents, working.key, cache, projectId, sessionId, onAccess]);
  if (denied || revoked) return <UnavailableView detail="View unavailable. Access may have ended or the resource may have been removed." onRetry={() => { void cache.resetQueries({ queryKey, exact: true }); setDenied(false); }} />;
  if (!query.data) return <div className="working-empty"><p>{query.isError ? 'Access could not be checked.' : 'Checking access…'}</p>{query.isError && <Button onClick={() => void query.refetch()}>Recheck access</Button>}</div>;
  return <>{children}</>;
}
