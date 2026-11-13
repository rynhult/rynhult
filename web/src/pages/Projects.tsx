import { DraftSaveNote } from '../lib/useUnsaved';
import { ProjectDirectory } from "../components/shell/ProjectDirectory";
import { ProjectDeleteDialog } from "../components/ProjectDelete";

import { useSingleMutation as useMutation } from "../lib/mutations";
import { useEffect, useRef, useState } from "react";
import { clearDraft, loadDraft, saveDraft } from "../lib/drafts";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { MoreHorizontal } from "lucide-react";
import { useConfirm } from "../components/confirm";

import { lineClass } from "../components/Changes";
import { useInstance, useRole } from "../lib/queries";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, post } from "../lib/api";
import type { ComposeReview, Container, Project } from "../lib/types";
import { Badge, Button, Dialog, Dot, ErrorNote, Field, Input, Popover, Select } from "../components/ui";
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
  useEffect(() => { if (JSON.stringify(form) !== JSON.stringify(blank)) saveDraft(storeKey, form, () => setForm(blank)); else clearDraft(storeKey); }, [storeKey, form]);
  const nodes = useNodes(open && owner);
  const slug = form.slug.toLowerCase().replace(/[^a-z0-9-]/g, "-");
  const machineAccounts = form.host === "local" ? [] : ((nodes.data ?? []).find((n) => n.name === form.host)?.accounts ?? []);
  const create = useMutation({
    mutationFn: () => post<Project>("/api/projects", {
      ...form, slug, repo: form.repo || null, workspace_path: form.workspace_path || `${root}/${slug}`,
      account: form.host !== "local" && form.account ? form.account : null,
    }),
    onSuccess: () => { clearDraft(storeKey); setForm(blank); queryClient.invalidateQueries({ queryKey: ["projects"] }); queryClient.invalidateQueries({ queryKey: ["source-directory"] }); onClose(); },
  });
  const duplicateDetail = create.error instanceof ApiError && create.error.data && typeof create.error.data === "object"
    ? (create.error.data as { detail?: { message?: string; existing_project_id?: number } }).detail : undefined;
  const duplicate = duplicateDetail?.existing_project_id;
  return (
    <Dialog open={open} onClose={onClose} title="Register a folder" description="A folder that already exists. Rynhult doesn't create or move it." dismissible={false}>
      <div className="space-y-4"><DraftSaveNote id={storeKey} />
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
        <ErrorNote error={duplicateDetail?.message ? new Error(duplicateDetail.message) : create.error} action={duplicate
          ? <Link to={`/projects/${duplicate}`} onClick={onClose}>Open the existing project</Link>
          : <Link to="/settings/security/folders">Open Allowed folders in Security</Link>} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.name.trim() || !slug || create.isPending} onClick={() => create.mutate()}>Register</Button>
        </div>
      </div>
    </Dialog>
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
export function Runtime({ project, part = "all", onlyInView = false }: {
  project: Project; part?: "all" | "status" | "autostart"; onlyInView?: boolean;
}) {
  const queryClient = useQueryClient();
  const [reviewing, setReviewing] = useState(false);
  const element = useRef<HTMLDivElement>(null);
  const [inView, setInView] = useState(!onlyInView);
  useEffect(() => {
    if (!onlyInView || !element.current) return;
    if (typeof IntersectionObserver === "undefined") { setInView(true); return; }
    const observer = new IntersectionObserver(entries => setInView(entries.some(entry => entry.isIntersecting)));
    // A visible row loads even when its controls need horizontal scrolling, so
    // keyboard users can reach an enabled Autostart control as before.
    observer.observe(element.current.closest("tr, section") ?? element.current);
    return () => observer.disconnect();
  }, [onlyInView]);
  const runtime = useQuery({
    queryKey: ["runtime", project.id],
    queryFn: () => api<{ containers: Container[]; note?: string; not_started?: boolean; compose?: ComposeReview }>(`/api/projects/${project.id}/runtime`),
    refetchInterval: 15000,
    refetchIntervalInBackground: false,
    enabled: !onlyInView || inView,
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
    <div ref={element} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 text-xs">
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

  return <ShellProjects />;
}
function ShellProjects() {
  const [deleting, setDeleting] = useState<Project | null>(null);
  const client = useQueryClient();
  const confirm = useConfirm();
  const archive = useMutation({ mutationFn: (id: number) => post(`/api/projects/${id}/archive`),
    onSettled: () => Promise.all([client.invalidateQueries({ queryKey: ["projects"] }), client.invalidateQueries({ queryKey: ["source-directory"] })]) });
  const [creating, setCreating] = useState(false);
  const [manualAdding, setAdding] = useState(false);
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();
  const adding = manualAdding || search.get('import') === 'existing';
  const closeImport = () => { setAdding(false); const next = new URLSearchParams(search); next.delete('import'); setSearch(next, { replace: true }); };
  return <>
    <ProjectDirectory onCreate={() => setCreating(true)} onRegister={() => setAdding(true)} actions={p =>
      <Popover menu label={`Project actions for ${p.name}`} trigger={<MoreHorizontal size={16} aria-hidden="true" />}>{close => <>
        {!p.archived && <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" disabled={archive.isPending} onClick={async () => {
          close();
          if (await confirm({ title: `Archive ${p.name}?`, body: "It leaves the launch lists. Its workspace, containers and repository are left exactly as they are.", confirm: "Archive", danger: true })) archive.mutate(p.id);
        }}>Archive</Button>}
        <Button role="menuitem" tabIndex={-1} size="sm" variant="ghost" onClick={() => { close(); setDeleting(p); }}>Delete</Button>
      </>}</Popover>} />
    <ErrorNote error={archive.error} />
    {deleting && <ProjectDeleteDialog project={deleting} onClose={() => setDeleting(null)} />}
    <NewProject open={adding} onClose={closeImport} />
    <NewProjectDialog open={creating} onClose={() => setCreating(false)} onCreated={made => navigate(`/projects/${made.id}`)} />
  </>;
}
