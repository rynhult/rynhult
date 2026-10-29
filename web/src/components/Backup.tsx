import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, Download, GitBranch, RefreshCw } from "lucide-react";
import { api, post } from "../lib/api";
import { ago, size } from "../lib/format";
import type { BackupView, Project } from "../lib/types";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, Meta, QueryState } from "./ui";

/** The project's git backup on the main Rynhult: on/off, when it last ran, what it holds, and the bundle to restore from. */
export function BackupCard({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const [chosen, setChosen] = useState<boolean | null>(null);  // the switch follows a click at once
  const key = ["backup", project.id];
  const backup = useQuery({ queryKey: key, queryFn: () => api<BackupView>(`/api/projects/${project.id}/backup`), refetchInterval: 60000 });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => post<Project>(`/api/projects/${project.id}/backup`, { enabled }),
    onMutate: (enabled) => queryClient.setQueryData<BackupView>(key, (d) => d && { ...d, enabled }),
    onSettled: () => { setChosen(null); queryClient.invalidateQueries({ queryKey: ["projects"] }); queryClient.invalidateQueries({ queryKey: key }); },
  });
  const run = useMutation({
    mutationFn: () => post<BackupView>(`/api/projects/${project.id}/backup/run`),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  });
  const enabled = chosen ?? backup.data?.enabled ?? project.git_backup;
  return (
    <Card>
      <CardHeader title="Backup" icon={DatabaseBackup} subtitle="Every branch and tag, copied to the main Rynhult; nothing there is ever rewritten or deleted"
        action={
          <label className="flex items-center gap-2 text-[13px] text-muted">
            <input type="checkbox" className="size-3.5 accent-[var(--accent)]" checked={enabled} disabled={toggle.isPending}
              onChange={(e) => { setChosen(e.target.checked); toggle.mutate(e.target.checked); }} aria-label="Back this project up to the main Rynhult" />
            On
          </label>
        } />
      <QueryState query={backup}>
        {(data) => <BackupBody data={data} project={project} />}
      </QueryState>
      {backup.data?.configured && enabled && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
            <RefreshCw size={13} aria-hidden="true" className={run.isPending ? "animate-spin" : undefined} /> {run.isPending ? "Backing up…" : "Back up now"}
          </Button>
          {backup.data?.repo?.branches.length ? (
            <a href={`/api/projects/${project.id}/backup/bundle`} download
              className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong bg-panel-2 px-2.5 text-[12.5px] font-medium hover:bg-panel-3 underline underline-offset-2">
              <Download size={13} aria-hidden="true" /> Download bundle
            </a>
          ) : null}
        </div>
      )}
      {(toggle.error || run.error) ? <div className="px-4 pb-3"><ErrorNote error={toggle.error ?? run.error} /></div> : null}
    </Card>
  );
}

function BackupBody({ data, project }: { data: BackupView; project: Project }) {
  if (!data.configured) return <Empty icon={DatabaseBackup}>Backups need this Rynhult's https address (ACC_BACKUP_URL in /etc/default/accd).</Empty>;
  if (!data.enabled) return <p className="px-4 py-3 text-[13px] text-muted">Off. {data.repo ? "What was backed up before is kept." : "Turn it on to keep a copy of every branch here."}</p>;
  if (data.git === false) return <Empty icon={GitBranch}>The project's folder isn't a git repository yet.</Empty>;
  const repo = data.repo;
  return (
    <div className="space-y-3 px-4 py-3">
      {data.error && <p role="alert" className="text-[13px] text-bad">Last attempt failed{data.failed_at ? ` ${ago(data.failed_at)}` : ""}: {data.error}</p>}
      <Meta items={[
        ["Last backup", data.last_at ? <span title={data.last_at}>{ago(data.last_at)}</span> : <span className="text-muted">{data.note ?? "Not yet"}</span>],
        ["Holds", repo ? <span className="tnum">{repo.branches.length} branch{repo.branches.length === 1 ? "" : "es"} · {repo.tags} tag{repo.tags === 1 ? "" : "s"} · {size(repo.size)}</span> : "–"],
        ["Checked", data.checked_at ? ago(new Date(data.checked_at * 1000).toISOString()) : "–"],
        ...(project.host !== "local" ? [["Pushed from", project.host] as [string, string]] : []),
      ]} />
      {repo && repo.branches.length > 0 && (
        <div className="flex flex-wrap gap-1.5" aria-label="Branches in the backup">
          {repo.branches.slice(0, 12).map((br) => <Badge key={br.name}><GitBranch size={11} aria-hidden="true" /> {br.name}</Badge>)}
          {repo.branches.length > 12 && <span className="text-xs text-faint">+{repo.branches.length - 12} more</span>}
        </div>
      )}
      {data.kept?.length ? (
        <div className="text-[13px]">
          <p className="text-wait">Rewritten upstream, kept under a new name:</p>
          <ul className="mt-1 space-y-0.5">
            {data.kept.slice(0, 5).map((k) => (
              <li key={k.as} className="text-muted"><span className="font-mono text-fg">{k.ref.replace(/^refs\/(heads|tags)\//, "")}</span> → <span className="font-mono">{k.as.replace(/^refs\/(heads|tags)\//, "")}</span> · {ago(k.at)}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
