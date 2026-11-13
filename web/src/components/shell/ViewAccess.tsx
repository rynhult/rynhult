import { useEffect, useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { useWorkingView } from '../../lib/workingView';
import type { Project, SessionDetail } from '../../lib/types';
import { ErrorNote } from '../ui';
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
  // Check a saved file's parent even before its editor is mounted. A listing
  // confirms deletion without loading hidden file contents; network errors do not.
  const file = url.searchParams.get('file');
  const base = `/api/files/${projectId ? 'project' : url.pathname.startsWith('/workspaces/') ? 'workspace' : 'session'}/${projectId ?? sessionId}`;
  const parent = file?.includes('/') ? file.slice(0, file.lastIndexOf('/')) : '';
  const listing = useQuery<{ entries: { name: string }[] }>({
    queryKey: ['files-tree', base, parent], queryFn: () => api(`${base}?path=${encodeURIComponent(parent)}`),
    enabled: !!file && query.isSuccess && !denied, retry: false, refetchInterval: 30000,
  });
  const fileMissing = !!file && (listing.isSuccess && !listing.data.entries.some(entry => entry.name === file.split('/').pop())
    || listing.error instanceof ApiError && [403, 404].includes(listing.error.status));
  const missing = !!query.data && (Array.isArray(query.data) ? !query.data.some(item => String(item.id) === projectId) : !allowArchived && query.data.observed_state === 'archived');
  useEffect(() => { if (allowArchived && query.data && !Array.isArray(query.data) && query.data.observed_state === 'archived') setDenied(false); }, [allowArchived, query.data]);
  const revoked = missing || query.error instanceof ApiError && [401, 403, 404].includes(query.error.status);
  useEffect(() => {
    if (revoked) {
      setDenied(true);
      if (sessionId) working.terminals.revokeSession(sessionId);
      const prefixes = projectId ? [`/api/files/project/${projectId}`] : [`/api/files/session/${sessionId}`, `/api/files/workspace/${sessionId}`];
      cache.removeQueries({ predicate: item => prefixes.includes(String(item.queryKey[1])) || item.queryKey[0] === 'sessions' && item.queryKey[1] === sessionId });
    }
    // Pruning owns clean missing files; editors retain unsaved buffers.
    onAccess(denied || revoked || fileMissing ? false : query.isSuccess ? true : undefined);
  }, [query.isSuccess, denied, revoked, fileMissing, working.documents, working.key, cache, projectId, sessionId, onAccess]);
  if (denied || revoked) return <UnavailableView back={projectId ? "/projects" : "/sessions"} detail="View unavailable. Access may have ended or the resource may have been removed." onRetry={() => { void cache.resetQueries({ queryKey, exact: true }); setDenied(false); }} />;
  if (!query.data) return <div className="working-empty">{query.isError ? <ErrorNote error={query.error} title={projectId ? "Couldn't load this project." : "Couldn't load this session."} onRetry={() => void query.refetch()} /> : <p>Checking access…</p>}</div>;
  return <>{children}</>;
}
