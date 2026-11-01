import { ProjectDeleteDialog } from "../components/ProjectDelete";
import { CollectionTable, CollectionView, useCollectionView } from "../components/CollectionView";
import { desktopBuild } from "../lib/desktop";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useEffect, useState } from "react";
import { clearDraft, loadDraft, saveDraft } from "../lib/drafts";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Archive as ArchiveIcon, ArrowRight, FolderGit2, FolderPlus, MoreHorizontal, Plus, ShieldCheck } from "lucide-react";
import { useConfirm } from "../components/confirm";
import { isLive } from "../components/session-bits";
import { useNewSession } from "../components/newSession";
import { needsYou } from "../lib/order";
import { lineClass } from "../components/Changes";
import { useInstance, useProjects, useRole, useSessions } from "../lib/queries";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, post } from "../lib/api";
import type { ComposeReview, Container, Project } from "../lib/types";
import { Badge, Button, Card, Dialog, Dot, Empty, ErrorNote, Field, Input, Page, PageHeader, Popover, SearchInput, Select, Skeleton, Toolbar } from "../components/ui";
import { initials, isLocalProject } from "../lib/format";
import { useNodes } from "../components/Machines";
import { NewProjectDialog } from "../components/NewProject";

/** Register a folder that already exists (or one on another machine): the advanced path; New project makes one. */
function NewProject({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { owner, username } = useRole();
  const instance = useInstance();
  const root = instance.workspaces_root ?? "/srv/acc/workspaces";
  const blank = { slug: "", name: "", workspace_path: "", repo: "", import_mode: "managed", host: "local", account: "" };
  const storeKey = `import-folder:${username ?? 'current'}`;
  const [form, setForm] = useState(() => loadDraft<typeof blank>(storeKey) ?? blank);
  useEffect(() => { saveDraft(storeKey, form); }, [storeKey, form]);
  const nodes = useNodes(open && owner);
  const slug = form.slug.toLowerCase().replace(/[^a-z0-9-]/g, "-");
  const machineAccounts = form.host === "local" ? [] : ((nodes.data ?? []).find((n) => n.name === form.host)?.accounts ?? []);
  const create = useMutation({
    mutationFn: () => post<Project>("/api/projects", {
      ...form, slug, repo: form.repo || null, workspace_path: form.workspace_path || `${root}/${slug}`,
      account: form.host !== "local" && form.account ? form.account : null,
    }),
    onSuccess: () => { clearDraft(storeKey); setForm(blank); queryClient.invalidateQueries({ queryKey: ["projects"] }); onClose(); },
  });
  return (
    <Dialog open={open} onClose={onClose} title="Register a folder" description="A folder that already exists. Rynhult doesn't create or move it." dismissible={false}>
      <div className="space-y-4">
        <p className="text-sm text-muted">Use where it is: no move, clone, Git initialization, commit or identity changes. Unregistering leaves the original folder intact.</p>
        {owner ? <Link to={`/settings/security/folders?machine=${encodeURIComponent(form.host)}&account=${encodeURIComponent(form.account || machineAccounts[0] || instance.agent_user)}`}>Allowed folders in Security</Link>
          : <p className="text-sm text-muted">Ask an owner to allow this folder if the agent account cannot use it.</p>}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><Input value={form.name} maxLength={200} onChange={(e) => setForm({ ...form, name: e.target.value, slug: form.slug || e.target.value })} autoFocus /></Field>
          <Field label="Slug"><Input value={slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} /></Field>
        </div>
        {(nodes.data ?? []).some((n) => n.status === "active") && (
          <Field label="Machine" hint="Where its sessions run. Its folder is on that machine.">
            <Select value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value, account: "" })}>
              <option value="local">{instance.host} · main</option>
              {(nodes.data ?? []).filter((n) => n.status === "active").map((n) => <option key={n.id} value={n.name}>{n.name}{n.connected ? "" : " (offline)"}</option>)}
            </Select>
          </Field>
        )}
        {machineAccounts.length > 1 && (
          <Field label="Agent account" hint={`Which of ${form.host}'s agent accounts runs this project's sessions.`}>
            <Select value={form.account} onChange={(e) => setForm({ ...form, account: e.target.value })}>
              <option value="">Choose…</option>
              {machineAccounts.map((a) => <option key={a} value={a}>{a}</option>)}
            </Select>
          </Field>
        )}
        <Field label="Workspace path" hint={form.host === "local" ? `An existing folder the selected agent account is allowed and able to use. The default workspace is ${root}.` : `A folder on ${form.host} that its agent account can use (e.g. /srv/acc/workspaces/${slug || "<slug>"}).`}>
          <Input value={form.workspace_path} placeholder={`${root}/${slug || "<slug>"}`} onChange={(e) => setForm({ ...form, workspace_path: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="GitHub repo (optional)"><Input value={form.repo} placeholder="owner/name" onChange={(e) => setForm({ ...form, repo: e.target.value })} /></Field>
          <Field label="Mode">
            <Select value={form.import_mode} onChange={(e) => setForm({ ...form, import_mode: e.target.value })}>
              <option value="managed">Let agents work here</option>
              <option value="readonly">Review only</option>
            </Select>
          </Field>
        </div>
        <ErrorNote error={create.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.name.trim() || !slug || create.isPending} onClick={() => create.mutate()}>Register</Button>
        </div>
      </div>
    </Dialog>
  );
}

function TrustButton({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const { agent_user } = useInstance();
  const [open, setOpen] = useState(false);
  const trust = useMutation({
    mutationFn: () => post<Project>(`/api/projects/${project.id}/trust`),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["projects"] }); setOpen(false); },
  });
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)}><ShieldCheck size={13} aria-hidden="true" /> Trust</Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Trust ${project.name}?`}>
        <div className="space-y-3 text-[13px] text-muted">
          <p>Agents launched here can read, edit and run files in <span className="font-mono text-fg">{project.workspace_path}</span>, within the permissions you pick per session.</p>
          <p>Sessions then start here without each agent asking "Do you trust this folder?". Rynhult never answers that question for you by typing into the terminal; it records your answer in the agents' own settings.</p>
          <details className="text-xs">
            <summary className="cursor-pointer text-muted hover:text-fg">Exactly what Rynhult changes</summary>
            <ul className="mt-2 list-disc space-y-1 pl-5 font-mono text-[12px]">
              <li>~{agent_user}/.claude.json → projects[path].hasTrustDialogAccepted</li>
              <li>~{agent_user}/.codex/config.toml → [projects."path"] trust_level = "trusted"</li>
            </ul>
          </details>
          <ErrorNote error={trust.error} />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" disabled={trust.isPending} onClick={() => trust.mutate()}>Trust this workspace</Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

/** Shows what an owner approves before autostart may run a stack: its compose files, or what changed in them. */
function ApproveCompose({ project, review, open, onClose }: { project: Project; review?: ComposeReview; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const approve = useMutation({
    mutationFn: () => post<Project & { note?: string }>(`/api/projects/${project.id}/runtime`, { compose_autostart: true, digest: review?.digest }),
    onSuccess: () => { onClose(); },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      queryClient.invalidateQueries({ queryKey: ["runtime", project.id] });
    },
  });
  const lines = (review?.diff ?? "").replace(/\n$/, "").split("\n").filter((l) => !l.startsWith("--- "));
  const first = !review?.by;
  return (
    <Dialog open={open} onClose={onClose} wide title={first ? `Approve ${project.name}'s compose files?` : `${project.name}'s compose files changed`}
      description={first ? "Autostart runs exactly these files." : `Approved by ${review?.by}${review?.at ? ` on ${new Date(review.at).toLocaleString()}` : ""}. Autostart holds the stack until an owner approves the change.`}
      footer={<>
        <ErrorNote error={approve.error} />
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!review || approve.isPending} onClick={() => approve.mutate()}>
          {project.compose_autostart ? "Approve" : "Approve and turn on autostart"}
        </Button>
      </>}>
      <div className="space-y-3 text-[13px] text-muted">
        <p>The stack runs under the projects' Docker account, which can reach that person's own files, and agents in this project can edit these files. Check for <span className="font-mono text-fg">volumes</span>, <span className="font-mono text-fg">privileged</span>, <span className="font-mono text-fg">network_mode: host</span> and images you don't expect.</p>
        {review?.note && <p className="text-wait">{review.note}</p>}
        {lines.length > 0 && lines[0] !== "" && (
          <pre tabIndex={0} role="region" aria-label="Compose approval" className="max-h-[28rem] overflow-auto rounded-lg border border-line bg-panel-2/40 py-2 font-mono text-[12px] leading-[1.45]">
            {lines.map((line, i) => line.startsWith("+++ ")
              ? <div key={i} className="px-4 pb-0.5 pt-1.5 font-sans text-[12px] font-medium text-fg first:pt-0">{line.slice(4).replace(/^now\//, "") === "/dev/null" ? "removed file" : line.slice(4).replace(/^now\//, "")}</div>
              : <div key={i} className={clsx("whitespace-pre px-4", lineClass(line))}>{line || " "}</div>)}
          </pre>
        )}
        {review?.truncated && <p className="text-wait">The change is longer than shown: read the files in the project before approving.</p>}
      </div>
    </Dialog>
  );
}

/** Container status for a project's compose stack, plus the autostart switch. */
export function Runtime({ project, part = "all" }: { project: Project; part?: "all" | "status" | "autostart" }) {
  const queryClient = useQueryClient();
  const [reviewing, setReviewing] = useState(false);
  const runtime = useQuery({
    queryKey: ["runtime", project.id],
    queryFn: () => api<{ containers: Container[]; note?: string; not_started?: boolean; compose?: ComposeReview }>(`/api/projects/${project.id}/runtime`),
    refetchInterval: 15000,
  });
  const review = runtime.data?.compose;
  const turnOff = useMutation({
    mutationFn: () => post(`/api/projects/${project.id}/runtime`, { compose_autostart: false }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const turnOn = useMutation({
    mutationFn: () => post(`/api/projects/${project.id}/runtime`, { compose_autostart: true, digest: review?.digest }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const { owner } = useRole();
  const containers = (runtime.data?.containers ?? []).filter((c) => !(c.State === "exited" && c.Status?.startsWith("Exited (0)")));
  const notStarted = !!runtime.data?.not_started;  // members: Rynhult doesn't run this stack, so it isn't looked at
  const noStack = !containers.length && (notStarted || !!runtime.data?.note?.includes("no compose file"));
  const unknown = runtime.isError || (!containers.length && !!runtime.data?.note && !noStack);
  const running = containers.filter((c) => c.State === "running").length;
  const tone = unknown || !runtime.data ? "neutral" : noStack ? "neutral" : !containers.length ? "neutral" : running === containers.length ? "ok" : running ? "wait" : "bad";
  const label = !runtime.data && !runtime.isError ? "Checking…" : unknown ? "Status unavailable" : notStarted ? "Stack not run by Rynhult" : noStack ? "No compose stack"
    : containers.length ? `${running}/${containers.length} containers up` : "Stack not running";
  const held = project.compose_autostart && review && !review.approved;
  const toggle = () => {
    if (project.compose_autostart) turnOff.mutate();
    else if (review?.approved) turnOn.mutate();  // the files it approved before, unchanged
    else setReviewing(true);
  };
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 text-xs">
      {part !== "autostart" && <span className="inline-flex items-center gap-2 text-muted" title={unknown || notStarted ? runtime.data?.note ?? String(runtime.error) : containers.map((c) => `${c.Service}: ${c.Status}`).join("\n")}>
        <Dot tone={tone} /> {label}
      </span>}
      {part === "autostart" && (noStack || !owner) && <span>{noStack ? "Not available" : project.compose_autostart ? "On" : "Off"}</span>}
      {part !== "status" && !noStack && owner && (
        <span className="inline-flex items-center gap-2">
          {held && (
            <Button size="sm" onClick={() => setReviewing(true)}
              title={review?.by ? "Its compose files changed since an owner approved them, so autostart holds the stack"
                : "Autostart waits until an owner has read and approved its compose files"}>
              <Badge tone="wait">{review?.by ? "Changed" : "Not approved"}</Badge> Approve compose files
            </Button>
          )}
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-muted" title="Run docker compose up -d whenever Rynhult starts (after a reboot), while its compose files are the ones an owner approved">
            <input type="checkbox" checked={project.compose_autostart} disabled={!project.compose_autostart && !review} onChange={toggle} className="size-3.5 accent-[var(--accent)]" /> Autostart
          </label>
        </span>
      )}
      {(turnOn.error || turnOff.error) && <ErrorNote error={turnOn.error ?? turnOff.error} />}
      <ApproveCompose project={project} review={review} open={reviewing} onClose={() => setReviewing(false)} />
    </div>
  );
}

export function Projects() {
  const { view, toggle } = useCollectionView("projects");
  const { host } = useInstance();
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [manualAdding, setAdding] = useState(false);
  const [search, setSearch] = useSearchParams();
  const adding = manualAdding || search.get('import') === 'existing';
  const closeImport = () => { setAdding(false); const next = new URLSearchParams(search); next.delete('import'); setSearch(next, { replace: true }); };
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const projects = useProjects();
  const sessions = useSessions();
  const newSession = useNewSession();
  const archive = useMutation({
    mutationFn: (id: number) => post(`/api/projects/${id}/archive`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const actions = (p: Project) => (
    <div className={clsx("flex flex-wrap items-center gap-2", view === "cards" && "justify-between border-t border-line px-3 py-2")}>
      <Link to={`/projects/${p.id}`} className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[12.5px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2">
        Open <ArrowRight size={12} aria-hidden="true" />
      </Link>
      <div className="flex flex-wrap items-center gap-1">
        {!p.trusted && p.import_mode === "managed" && !p.archived && <TrustButton project={p} />}
        {p.import_mode === "managed" && !p.archived && <Button size="sm" onClick={() => newSession({ mode: "project", project_id: p.id })} aria-label={`New session in ${p.name}`}>
          <Plus size={13} aria-hidden="true" /> New session</Button>}
        <Popover menu label={`Project actions for ${p.name}`} trigger={<MoreHorizontal size={16} aria-hidden="true" />}>{close => <>
          {!p.archived && <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" aria-label={`Archive ${p.name}`} disabled={archive.isPending} onClick={async () => {
            close();
            if (await confirm({ title: `Archive ${p.name}?`, body: "It leaves the launch lists. Its workspace, containers and repository are left exactly as they are.", confirm: "Archive", danger: true })) archive.mutate(p.id);
          }}><ArchiveIcon size={13} aria-hidden="true" /> Archive</Button>}
          <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" aria-label={`Delete ${p.name}`} onClick={() => { close(); setDeleting(p); }}>Delete</Button>
        </>}</Popover>
      </div>
    </div>
  );
  const liveFor = (p: Project) => (sessions.data ?? []).filter(session => session.project_id === p.id && isLive(session));
  const runtimeAvailable = (p: Project) => !p.machine_offline && p.import_mode === "managed" && !p.archived;
  const visible = (projects.data ?? []).filter((p) => !isLocalProject(p) && !p.private_workspace && (showArchived || !p.archived)  // local: see Workspaces
    && (!query || `${p.name} ${p.slug} ${p.repo ?? ""}`.toLowerCase().includes(query.toLowerCase())));
  return (
    <Page wide>
      <PageHeader title="Projects" description={desktopBuild ? "Projects, files and session history on this computer." : `Each project is a folder on ${host} that your agent sessions work in. Its page also shows its GitHub repository, checks and Docker stack, when it has them.`}
        actions={<>
          <Button onClick={() => setAdding(true)}><Plus size={14} aria-hidden="true" /> Register folder</Button>
          <Button variant="primary" onClick={() => setCreating(true)}><FolderPlus size={14} aria-hidden="true" /> New project</Button>
        </>} />
      <Toolbar>
        {toggle}
        <SearchInput value={query} onChange={setQuery} placeholder="Filter projects" className="w-64" />
        <label className="inline-flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} className="size-3.5 accent-[var(--accent)]" /> Show archived
        </label>
      </Toolbar>
      <ErrorNote error={archive.error} />
      {projects.data === undefined ? (projects.error ? <ErrorNote error={projects.error} /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-44 rounded-[10px]" />)}</div>
      )) : visible.length ? (view === "list" ? (
        <CollectionTable label="Projects" items={visible} itemKey={p => p.id} columns={[
          { key: "name", label: "Name", sortValue: p => p.name, render: p => <div className="min-w-[10rem] space-y-2">
            <Link to={`/projects/${p.id}`} title={p.workspace_path} className="font-semibold text-fg underline underline-offset-2 hover:text-accent [overflow-wrap:anywhere]">{p.name}</Link>
            <div className="flex flex-wrap gap-1.5">{p.archived && <Badge>archived</Badge>}{p.import_mode === "readonly" ? <Badge tone="wait">read-only</Badge> : !p.trusted && <Badge tone="wait">not trusted</Badge>}</div>
          </div> },
          { key: "folder", label: "Repository / folder", sortValue: p => p.repo ?? p.workspace_path, render: p => <div className="space-y-1 [overflow-wrap:anywhere]">
            {p.repo && <a href={`https://github.com/${p.repo}`} target="_blank" rel="noreferrer" className="text-accent underline underline-offset-2">{p.repo}</a>}
            <div title={p.workspace_path} className="line-clamp-2 font-mono text-xs">{p.host && p.host !== "local" ? `${p.host}: ` : ""}{p.workspace_path}</div>
          </div> },
          { key: "live", label: "Live sessions", sortValue: p => liveFor(p).length, render: p => sessions.data === undefined ? (sessions.error ? "Sessions unavailable" : "Checking…") : <div className="space-y-1">
            <span>{liveFor(p).length ? `${liveFor(p).length} live` : "No live sessions"}</span>
            {needsYou(liveFor(p)).length > 0 && <p className="text-wait">{needsYou(liveFor(p)).length} needs you</p>}
          </div> },
          { key: "runtime", label: "Containers / compose", render: p => runtimeAvailable(p) ? <Runtime project={p} part="status" /> : p.machine_offline ? "Machine offline" : p.archived ? "Archived" : "Read-only" },
          { key: "autostart", label: "Autostart", sortValue: p => Number(p.compose_autostart), render: p => runtimeAvailable(p) ? <Runtime project={p} part="autostart" /> : "Not available" },
          { key: "actions", label: "Actions", render: p => actions(p) },
        ]} />
      ) : (
        <CollectionView view={view}>
          {visible.map((p) => {
            const live = (sessions.data ?? []).filter((s) => s.project_id === p.id && isLive(s));
            const waiting = needsYou(live).length;
            return (
              <Card key={p.id} className={clsx("flex min-w-0 flex-col", p.archived && "opacity-80")}>
                <div className="flex items-start gap-3 px-4 pb-3 pt-4">
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-panel-2 text-[13px] font-semibold uppercase text-muted" aria-hidden="true">
                    {initials(p.name)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <Link to={`/projects/${p.id}`} title={p.workspace_path} className="block truncate py-0.5 text-[14px] font-semibold leading-6 text-fg hover:text-accent underline underline-offset-2">{p.name} <ArrowRight size={12} className="inline" aria-hidden="true" /></Link>
                    <div className="truncate text-xs text-muted">
                      {p.repo ? <a className="inline-block py-1 hover:text-fg underline underline-offset-2" href={`https://github.com/${p.repo}`} target="_blank" rel="noreferrer">{p.repo}</a> : p.slug}
                    </div>
                  </div>
                </div>
                {/* Live state leads (UX review PJ-2); the folder is in the name's tooltip, and only exceptions get a badge. */}
                <p className="flex items-center gap-1.5 px-4 text-[12.5px]">
                  {waiting > 0 && <><Dot tone="wait" /><span className="text-wait">{waiting} needs you</span>{live.length > waiting && <span className="text-faint">·</span>}</>}
                  {live.length > waiting && <span className="text-muted">{live.length - waiting} live</span>}
                  {!live.length && <span className="text-faint">No live sessions</span>}
                </p>
                {(p.archived || p.import_mode === "readonly" || !p.trusted) && <div className="flex flex-wrap gap-1.5 px-4 pt-3">
                  {p.archived && <Badge>archived</Badge>}
                  {p.import_mode === "readonly" ? <Badge tone="wait" title="Rynhult shows it but launches nothing here">read-only</Badge>
                    : !p.trusted && <Badge tone="wait" title="Agents will ask whether to trust this folder; trust it here or from New session">not trusted</Badge>}
                </div>}
                <div className="mt-auto space-y-3 px-4 pb-3 pt-4">
                  {p.machine_offline && <p className="text-xs text-muted">Machine offline</p>}
                  {!p.machine_offline && p.import_mode === "managed" && !p.archived && <Runtime project={p} />}
                </div>
                {actions(p)}
              </Card>
            );
          })}
        </CollectionView>
      )) : <Card>{query ? <Empty icon={FolderGit2}>No projects match.</Empty> : (
        <Empty icon={FolderGit2} action={<Button variant="primary" onClick={() => setCreating(true)}><FolderPlus size={14} aria-hidden="true" /> New project</Button>}>
          No projects yet. Start one from an empty folder or a repository: Rynhult makes the folder for you.
        </Empty>)}</Card>}
      <NewProject open={adding} onClose={closeImport} />
      {/* A new project opens its page, where New session is the next step (first-run review). */}
      {deleting && <ProjectDeleteDialog project={deleting} onClose={() => setDeleting(null)} />}
      <NewProjectDialog open={creating} onClose={() => setCreating(false)} onCreated={(made) => navigate(`/projects/${made.id}`)} />
    </Page>
  );
}
