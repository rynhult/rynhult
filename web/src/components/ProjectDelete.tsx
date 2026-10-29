import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { api, post } from '../lib/api';
import type { Project } from '../lib/types';
import { useSessions } from '../lib/queries';
import { isLive } from './session-bits';
import { Button, Dialog, ErrorNote, Field, Input, LoadingRows } from './ui';

interface Plan { remove_folder: boolean; path: string; folders: string[]; sessions_archived: number }

/** Shared by the project menu and page; failures keep the typed confirmation open. */
export function ProjectDeleteDialog({ project, onClose }: { project: Project; onClose: () => void }) {
  const [typed, setTyped] = useState('');
  const client = useQueryClient();
  const navigate = useNavigate();
  const sessions = useSessions();
  const address = `/api/projects/${project.id}/delete`;
  const plan = useQuery({ queryKey: ['project-delete', project.id], queryFn: () => api<Plan>(address), retry: false });
  const removeFolder = plan.data?.remove_folder ?? (project.managed_checkout && project.import_mode === "managed");
  const remove = useMutation({ mutationFn: () => post(address, { confirm: typed }), onSuccess: async () => {
    await Promise.all(['projects', 'sessions', 'ports', 'publishing'].map(key => client.invalidateQueries({ queryKey: [key] })));
    onClose(); navigate('/projects');
  } });
  const stop = useMutation({ mutationFn: async () => {
    const rows = (sessions.data ?? []).filter(s => s.project_id === project.id);
    for (const s of rows.filter(s => s.team_id === s.id)) await post(`/api/sessions/${s.id}/team/stop`);
    for (const s of rows.filter(s => isLive(s) && !s.team_id)) await post(`/api/sessions/${s.id}/stop`);
  }, onSuccess: async () => { await client.invalidateQueries({ queryKey: ['sessions'] }); await plan.refetch(); } });
  return <Dialog open onClose={onClose} dismissible={false} title={`Delete ${project.name}?`} footer={<>
    <Button variant="ghost" disabled={remove.isPending} onClick={onClose}>Cancel</Button>
    <Button variant="danger" disabled={!plan.data || plan.isFetching || typed !== project.slug || remove.isPending || stop.isPending}
      onClick={() => remove.mutate()}>{remove.isPending ? 'Deleting…' : 'Delete project'}</Button>
  </>}>
    <div className="space-y-4 text-[13px]">
      <p>This can't be undone. Removes the project record, settings, brief and port reservations. Sites must be unpublished and containers stopped first.</p>
      {plan.isPending && <LoadingRows />}
      {(removeFolder ? <div>
        <p>Also permanently deletes this managed checkout and its worktrees, including uncommitted files:</p>
        <ul className="list-disc pl-5">{(plan.data?.folders ?? [project.workspace_path]).map(path => <li key={path} className="break-all font-mono">{path}</li>)}</ul>
      </div> : <p>Keeps the registered or imported folder and its worktrees: <span className="break-all font-mono">{project.workspace_path}</span>. Only the Rynhult record is removed.</p>)}
      <p>Keeps the GitHub repository untouched. Sessions are archived and detached, with history retained. Audit entries, Inbox items and alerts stay. Backups follow their retention policy.</p>
      <ErrorNote error={plan.error ?? remove.error ?? stop.error} />
      {plan.isError && <div className="flex flex-wrap gap-2">
        <Button disabled={stop.isPending || sessions.isPending} onClick={() => stop.mutate()}>Stop all first</Button>
        <Button disabled={plan.isFetching} onClick={() => plan.refetch()}>Check again</Button>
      </div>}
      <Field label="Type the project slug to confirm" hint={project.slug}>
        <Input value={typed} onChange={event => setTyped(event.target.value)} autoComplete="off" spellCheck={false} />
      </Field>
    </div>
  </Dialog>;
}
