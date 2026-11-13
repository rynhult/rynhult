import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { FolderUp } from 'lucide-react';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { useRole } from '../lib/queries';
import { post } from '../lib/api';
import { workspaceName, workspaceTopic } from '../lib/format';
import type { Project, SessionDetail } from '../lib/types';
import { Button, Dialog, ErrorNote, Field, Input } from './ui';

export default function PromoteButton({ session }: { session: SessionDetail }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const folder = session.workdir?.split("/").pop() ?? "";
  const suggested = workspaceTopic(folder);
  const [open, setOpen] = useState(false);
  const { owner } = useRole();  // creating a repository uses the owner's GitHub token
  const [form, setForm] = useState({ name: workspaceName(folder), slug: suggested, description: "", github: owner, confirm: false });
  const promote = useMutation({
    mutationFn: () => post<Project>(`/api/sessions/${session.id}/promote`, form),
    onSuccess: (project) => { queryClient.invalidateQueries({ queryKey: ["projects"] }); setOpen(false); navigate(`/projects/${project.id}`); },
  });
  return (
    <>
      <Button onClick={() => setOpen(true)}><FolderUp size={14} aria-hidden="true" /> Promote to project</Button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Promote to project" dismissible={false}
        description="Worth pursuing? The workspace folder becomes a project, and its research, council reports and notes come with it."
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="primary" disabled={!form.name.trim() || !/^[a-z0-9][a-z0-9-]*$/.test(form.slug) || (form.github && !form.confirm) || promote.isPending}
            onClick={() => promote.mutate()}>{promote.isPending ? "Promoting…" : "Promote"}</Button>
        </>}>
        <div className="space-y-4">
          <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Project name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /></Field>
            <Field label="Slug" hint="Also the repository name"><Input value={form.slug} className="font-mono"
              onChange={(e) => setForm({ ...form, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} /></Field>
          </div>
          <label className="flex items-start gap-2.5 text-[13px] text-fg">
            <input type="checkbox" checked={form.github} onChange={(e) => setForm({ ...form, github: e.target.checked, confirm: false })} className="mt-0.5 size-3.5 accent-[var(--accent)]" />
            <span>Create a private GitHub repository, push this workspace's history to it, and protect main
              <span className="block text-xs text-muted">Projects use GitHub and CI/CD; untick to keep it local for now.</span></span>
          </label>
          {form.github && (
            <>
              <Field label="Description (optional)"><Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
              <label className="flex items-start gap-2 rounded-md border border-wait/30 bg-wait/10 p-3 text-xs text-wait">
                <input type="checkbox" checked={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.checked })} className="mt-0.5" />
                <span>Create <b>github.com/…/{form.slug || "…"}</b> (private) under your account and push to it.</span>
              </label>
            </>
          )}
          <p className="font-mono text-[11.5px] text-faint">{session.workdir}</p>
          <ErrorNote error={promote.error} />
        </div>
      </Dialog>
    </>
  );
}

