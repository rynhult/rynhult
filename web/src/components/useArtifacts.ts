import { useWorkingView } from '../lib/workingView';
import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { SessionArtifacts, SessionDetail } from '../lib/types';

/** The files the session's agent left in .acc/artifacts, polled like Changes: often during a turn, slowly when idle, and
 * once more when a turn ends. Shared by the tab and its count. */
export function useArtifacts(session: SessionDetail) {
  const view = useWorkingView();
  const working = ["active", "waiting_input"].includes(session.observed_state);
  const query = useQuery({
    queryKey: ["artifacts", session.id], queryFn: () => api<SessionArtifacts>(`/api/sessions/${session.id}/artifacts`),
    refetchInterval: working ? 10000 : 60000, retry: false, enabled: !view || view.visible,
  });
  const { refetch } = query;
  const wasWorking = useRef(working);
  useEffect(() => {
    if ((!view || view.visible) && wasWorking.current && !working) void refetch();
    wasWorking.current = working;
  }, [working, refetch]);
  return query;
}

