import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { api, post } from '../lib/api';
import { Button, ErrorNote } from './ui';

export type FirstCommitState = { needed: boolean; created: boolean };

/** Explicit local-only recovery; reading this notice never changes a repository. */
export function FirstCommit({ projectId, onDone }: { projectId: number; onDone?: () => void }) {
  const client = useQueryClient();
  const url = `/api/projects/${projectId}/first-commit`;
  const state = useQuery({ queryKey: ['first-commit', projectId], queryFn: () => api<FirstCommitState>(url) });
  const make = useMutation({ mutationFn: () => post<FirstCommitState>(url), onSuccess: data => {
    client.setQueryData(['first-commit', projectId], data);
    client.invalidateQueries({ queryKey: ['git-identity', projectId] });
    onDone?.();
  } });
  if (state.isPending) return null;
  if (state.error) return <ErrorNote error={state.error} onRetry={state.refetch} />;
  if (!state.data?.needed) return null;
  return <section aria-label="First Git commit" className="space-y-3 rounded-md border border-wait/30 bg-panel-2 p-4 text-sm">
    <p>This project has no Git commit yet. Sessions with their own worktree and teams need one first.</p>
    <p>Make a README.md with the project name using the configured Git author. The commit stays local; nothing is pushed.</p>
    <ErrorNote error={make.error} />
    <Button disabled={make.isPending} onClick={() => make.mutate()}>{make.isPending ? 'Committing…' : 'Make the first commit'}</Button>
  </section>;
}
