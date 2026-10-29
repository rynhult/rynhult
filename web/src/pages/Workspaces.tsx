import { CollectionTable, CollectionView, useCollectionView } from "../components/CollectionView";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { ArrowRight, BookOpen, FileText, FlaskConical, FolderGit2, MoreHorizontal, Plus, Scale, Trash2 } from "lucide-react";
import { api, post } from "../lib/api";
import { ago, workspaceName } from "../lib/format";
import type { ObservedState, Provider, Project } from "../lib/types";
import { Badge, Button, Card, Dialog, Dot, Empty, ErrorNote, Field, Input, Page, PageHeader, Popover, QueryState, Skeleton, Select } from "../components/ui";
import { sessionState } from "../components/session-bits";
import { useNewSession } from "../components/newSession";
import { useConfirm } from "../components/confirm";
import { useInstance, useRole } from "../lib/queries";

interface WorkspaceSession { id: string; name: string; provider: Provider; profile: string | null; observed_state: ObservedState; desired_state: string; last_output_at: string | null }
interface Workspace {
  folder: string; path: string; created_at: string; kind?: string;
  project: { id: number; name: string; repo: string | null; private_workspace?: boolean } | null;
  sessions: WorkspaceSession[];
  docs: Record<string, number> | null;
  folder_removed?: boolean;
}

function NewWorkspace({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [kind, setKind] = useState("research");
  const [name, setName] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const newSession = useNewSession();
  const create = useMutation({
    mutationFn: () => post<Project>("/api/workspaces", { kind: "development", name }),
    onSuccess: (p) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      onClose(); navigate(`/projects/${p.id}`);
    },
  });
  return <Dialog open={open} onClose={onClose} title="New workspace" dismissible={false}
    footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button>
      <Button variant="primary" disabled={create.isPending || (kind === "development" && !name.trim())} onClick={() => {
        if (kind === "development") create.mutate();
        else { onClose(); newSession({ mode: "workspace" }); }
      }}>{kind === "development" ? (create.isPending ? "Creating…" : "Create workspace") : "Choose session"}</Button></>}>
    <div className="space-y-4">
      <Field label="Kind"><Select value={kind} onChange={(e) => setKind(e.target.value)}>
        <option value="research">Research / Council</option><option value="development">Development</option>
      </Select></Field>
      {kind === "development" ? <>
        <Field label="Workspace name"><Input name="workspace-name" autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} /></Field>
        <p className="text-xs text-muted">Local git only, with a project's session tools and git backup. Promote it to add a remote repository.</p>
      </> : <p className="text-xs text-muted">Researcher and Council sessions share a local folder for findings and reports.</p>}
      <ErrorNote error={create.error} />
    </div>
  </Dialog>;
}

/** "beyblade-tournament-apps-20261018-2" -> { title: "Beyblade tournament apps", date: "Oct 18, 2026" } */
function describe(folder: string) {
  const match = folder.match(/^(.*)-(\d{4})(\d{2})(\d{2})(?:-\d+)?$/);
  const date = match ? new Date(`${match[2]}-${match[3]}-${match[4]}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
  return { title: workspaceName(folder), date };
}

/** Deleting removes the folder and its local git history for good, so the owner types the folder name. */
function DeleteWorkspace({ w, title, onClose }: { w: Workspace; title: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  const live = w.sessions.filter((x) => x.desired_state === "running").length;
  const remove = useMutation({
    mutationFn: () => post(`/api/workspaces/${encodeURIComponent(w.folder)}/delete`, { confirm: typed }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["workspaces"] }); queryClient.invalidateQueries({ queryKey: ["sessions"] }); onClose(); },
  });
  if (w.project) return null;  // a promoted workspace is a project's checkout now
  return (
      <Dialog open onClose={onClose} title={w.folder_removed ? `Remove ${title} from list?` : `Delete ${title}?`} dismissible={false}
        description={w.folder_removed ? "The folder is already removed. Remove its workspace entry and archive its sessions; their history is kept. No files are deleted." : "Removes the folder with its research, council reports, notes, wiki and local git history. This can't be undone."}
        footer={<>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="danger" disabled={typed !== w.folder || remove.isPending} onClick={() => remove.mutate()}>
            {remove.isPending ? "Removing…" : w.folder_removed ? "Remove from list" : "Delete workspace"}</Button>
        </>}>
        <div className="space-y-4 text-[13px] text-muted">
          <p>{w.sessions.length === 1 ? "Its session" : `Its ${w.sessions.length} sessions`}{live ? ` (${live} still running)` : ""} will be stopped and archived; they stay in the session history.</p>
          <Field label="Type the folder name to confirm" hint={<span className="font-mono">{w.folder}</span>}>
            <Input value={typed} onChange={(e) => setTyped(e.target.value)} className="font-mono" autoComplete="off" spellCheck={false} />
          </Field>
          <ErrorNote error={remove.error} />
        </div>
      </Dialog>
  );
}

function WorkspaceCard({ w, table = false }: { w: Workspace; table?: boolean }) {
  const newSession = useNewSession();
  const queryClient = useQueryClient();
  const { owner } = useRole();
  const confirm = useConfirm();
  const [deleting, setDeleting] = useState(false);
  const convert = useMutation({
    mutationFn: () => post(`/api/workspaces/${encodeURIComponent(w.folder)}/development?session=${w.sessions[0].id}`),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["workspaces"] }); queryClient.invalidateQueries({ queryKey: ["projects"] }); },
  });
  const { title: folderTitle, date } = describe(w.folder);
  const title = w.project?.private_workspace ? w.project.name : folderTitle;
  const development = w.kind === "development" && !!w.project;
  const latest = w.sessions[w.sessions.length - 1];
  const reports = (w.docs?.research ?? 0) + (w.docs?.council ?? 0) + (w.docs?.decisions ?? 0);
  const heading = (<div className="flex items-start gap-3 px-4 pb-3 pt-4">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-panel-2 text-muted" aria-hidden="true"><FlaskConical size={17} /></span>
        <div className="min-w-0 flex-1">
          <Link to={w.project ? `/projects/${w.project.id}` : latest ? `/sessions/${latest.id}` : `/workspaces/${w.folder}/wiki`} className="block truncate py-0.5 text-[14px] font-semibold leading-6 text-fg hover:text-accent underline underline-offset-2">{title} <ArrowRight size={12} className="inline" aria-hidden="true" /></Link>
          <div className="truncate text-xs text-muted">{date} · <span className="font-mono">{w.folder}</span></div>
          <div className="mt-2 flex flex-wrap gap-2">{w.folder_removed && <Badge>Folder already removed</Badge>}{development ? <Badge>Development · local only</Badge> : <Badge>Research / council</Badge>}{!development && w.project && <Badge title="Promoted to a project">promoted</Badge>}</div>
        </div>
      </div>);
  const sessionLinks = (<div className={table ? "space-y-1.5" : "space-y-1.5 px-4"}>
        {w.sessions.map((x) => {
          const st = sessionState(x);
          return (
            <Link key={x.id} to={`/sessions/${x.id}`} className="flex items-center gap-2.5 rounded-md py-1 text-[13px] hover:text-accent underline underline-offset-2">
              <Dot tone={st.tone} pulse={st.pulse} />
              <span className="truncate text-fg">{x.name}</span>
              {x.profile && <Badge>{x.profile}</Badge>}
              <span className="ml-auto shrink-0 text-xs text-faint">{st.label}{x.last_output_at ? ` · ${ago(x.last_output_at)}` : ""}</span>
            </Link>
          );
        })}
      </div>);
  const reportCounts = (<div className={table ? "flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted" : "mt-auto flex flex-wrap gap-x-4 gap-y-1 px-4 pb-3 pt-4 text-xs text-muted"}>
        {w.docs === null ? <span className="text-faint">Reports unavailable</span> : <>
          <span className="inline-flex items-center gap-1.5"><FileText size={12} aria-hidden="true" /> {w.docs.research ?? 0} research</span>
          <span className="inline-flex items-center gap-1.5"><Scale size={12} aria-hidden="true" /> {w.docs.council ?? 0} council</span>
          {!!w.docs.handoffs && <span>{w.docs.handoffs} notes</span>}
          {reports === 0 && <span className="text-faint">no reports yet</span>}
        </>}
      </div>);
  const actions = (<div className={table ? "flex flex-wrap items-center gap-2" : "flex flex-wrap items-center justify-between gap-2 border-t border-line px-3 py-2"}>
        {w.project ? (
          <Link to={`/projects/${w.project.id}`} className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1.5 rounded-md px-2 text-[12.5px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2">
            <FolderGit2 size={13} aria-hidden="true" /> {w.project.name} <ArrowRight size={12} aria-hidden="true" />
          </Link>
        ) : (
          <Link to={latest ? `/sessions/${latest.id}?tab=handoffs` : `/workspaces/${w.folder}/wiki`} className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[12.5px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2">
            {latest ? "Findings" : "Open wiki"} <ArrowRight size={12} aria-hidden="true" />
          </Link>
        )}
        <Popover menu className="ml-auto" label={`Workspace actions for ${title}`} trigger={<MoreHorizontal size={16} aria-hidden="true" />}>{close => <div className="space-y-1">
        {!w.folder_removed && (development || latest) && <Link role="menuitem" tabIndex={-1} onClick={close} to={development ? `/projects/${w.project!.id}?tab=files` : `/workspaces/${w.folder}/files?session=${latest.id}`} className="flex min-h-8 items-center rounded-md px-2 text-xs text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2">Files</Link>}
        <Link role="menuitem" tabIndex={-1} onClick={close} to={development ? `/projects/${w.project!.id}/wiki` : `/workspaces/${w.folder}/wiki`} className="flex min-h-8 items-center gap-1 rounded-md px-2 text-[12.5px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2">
          <BookOpen size={13} aria-hidden="true" /> Wiki
        </Link>
        {!w.folder_removed && <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" onClick={() => { close(); newSession(development ? { mode: "project", project_id: w.project!.id, worktree: true } : { mode: "workspace", workspace: w.folder, profile: "council" }); }}
          title="Start another session in this folder, e.g. Council to weigh the research">
          <Plus size={13} aria-hidden="true" /> Add session
        </Button>}
        {owner && !w.project && !w.folder_removed && <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" disabled={convert.isPending || !w.sessions.length} onClick={async () => {
          close();
          if (await confirm({ title: "Convert to Development?", body: "Stop its sessions first. The folder becomes a local-only development workspace with project capabilities. Start new sessions afterwards; older sessions cannot resume.", confirm: "Convert" })) convert.mutate();
        }}>Convert to Development</Button>}
        {!w.project && <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" aria-label={`${w.folder_removed ? "Remove from list" : "Delete workspace"} ${title}`} onClick={() => { close(); setDeleting(true); }}><Trash2 size={13} aria-hidden="true" /> {w.folder_removed ? "Remove from list" : "Delete workspace"}</Button>}
        </div>}</Popover>
      </div>);
  const dialogs = <>{deleting && <DeleteWorkspace w={w} title={title} onClose={() => setDeleting(false)} />}<ErrorNote error={convert.error} /></>;
  if (table) return <tr>
    <th scope="row" className="min-w-[12rem] font-normal"><div className="space-y-2">
      <Link to={w.project ? `/projects/${w.project.id}` : latest ? `/sessions/${latest.id}` : `/workspaces/${w.folder}/wiki`} className="font-semibold text-fg underline underline-offset-2 hover:text-accent [overflow-wrap:anywhere]">{title}</Link>
      <div className="flex flex-wrap gap-2">{w.folder_removed && <Badge>Folder already removed</Badge>}{development ? <Badge>Development · local only</Badge> : <Badge>Research / council</Badge>}{!development && w.project && <Badge>promoted</Badge>}</div>
    </div></th>
    <td><span title={w.path} className="line-clamp-2 font-mono text-xs [overflow-wrap:anywhere]">{w.path}</span><p className="mt-1 text-xs text-faint">{date}</p></td>
    <td><p className="mb-2 text-xs">{w.sessions.filter(session => session.desired_state === "running").length} live</p>{sessionLinks}</td>
    <td>{reportCounts}</td>
    <td><div className="min-w-[12rem] space-y-2">
      {actions}
      {!w.folder_removed && <Button size="sm" aria-label={`New session in ${title}`} onClick={() => newSession(development ? { mode: "project", project_id: w.project!.id, worktree: true } : { mode: "workspace", workspace: w.folder, profile: "council" })}><Plus size={13} aria-hidden="true" /> New session</Button>}
      {dialogs}
    </div></td>
  </tr>;
  return <Card className="flex min-w-0 flex-col">{heading}{sessionLinks}{reportCounts}{actions}{dialogs}</Card>;
}

export function Workspaces() {
  const { view, toggle } = useCollectionView("workspaces");
  const { host } = useInstance();
  const [creating, setCreating] = useState(false);
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: () => api<Workspace[]>("/api/workspaces"), refetchInterval: 10000 });
  return (
    <Page wide>
      <NewWorkspace open={creating} onClose={() => setCreating(false)} />
      <PageHeader title="Workspaces"
        description={`Private local-git folders on ${host}: Development builds an app, Research gathers evidence, Council compares agents' recommendations.`}
        actions={<>{toggle}<Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} aria-hidden="true" /> New workspace</Button></>} />
      <QueryState query={workspaces} isEmpty={(d) => !d.length}
        loading={<div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-52 rounded-[10px]" />)}</div>}
        empty={<Card><Empty icon={FlaskConical} action={<Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} aria-hidden="true" /> New workspace</Button>}>
          No workspaces yet. Create one for private development, research or council work.</Empty></Card>}>
        {(data) => view === "list" ? <CollectionTable label="Workspaces" items={data} itemKey={w => w.path}
          columns={[
            { key: "name", label: "Name", sortValue: w => w.project?.private_workspace ? w.project.name : describe(w.folder).title },
            { key: "folder", label: "Folder", sortValue: w => w.path },
            { key: "sessions", label: "Live sessions", sortValue: w => w.sessions.filter(session => session.desired_state === "running").length },
            { key: "reports", label: "Reports", sortValue: w => w.docs ? Object.values(w.docs).reduce((sum, count) => sum + count, 0) : -1 },
            { key: "actions", label: "Actions" },
          ]} renderRow={w => <WorkspaceCard key={w.path} w={w} table />} />
          : <CollectionView view={view}>{data.map((w) => <WorkspaceCard key={w.path} w={w} />)}</CollectionView>}
      </QueryState>
    </Page>
  );
}
