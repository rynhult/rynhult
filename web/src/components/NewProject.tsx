import { BriefDocuments, uploadDocuments } from "./BriefDocuments";
import { desktopBuild } from "../lib/desktop";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderPlus } from "lucide-react";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { api, post } from "../lib/api";
import { useInstance, useRole } from "../lib/queries";
import type { Project } from "../lib/types";
import { useNodes } from "./Machines";
import { Button, Dialog, ErrorNote, Field, Input, Segmented, Select, Textarea } from "./ui";

type Source = "empty" | "clone" | "github";
export const projectSlug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);

/** A new project without a server shell (UX review 2, first run): an empty folder, a clone of a repository the
 *  agent account can reach, or (owners, with Rynhult's GitHub token) a new private GitHub repository. The folder is always
 *  <your workspaces folder>/<slug>; trust for agents is part of the same step: on for an empty folder, off for a clone
 *  (decision 0008 follow-up), since trust lets agents run with the repository's own config and hooks. Owners with other
 *  machines choose where: the folder is then made there, through that machine's broker, under its account's root. */
export function NewProjectDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated?: (project: Project) => void }) {
  const queryClient = useQueryClient();
  const instance = useInstance();
  const { owner } = useRole();
  const github = owner && !!instance.github;
  const [documentFiles, setDocumentFiles] = useState<File[]>([]);
  const [createdProject, setCreatedProject] = useState<Project | null>(null);
  const [source, setSource] = useState<Source>("empty");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [trust, setTrust] = useState(true);
  const [confirmGithub, setConfirmGithub] = useState(false);
  const [host, setHost] = useState("local");
  const [account, setAccount] = useState("");
  const nodes = useNodes(open && owner);
  const machines = (nodes.data ?? []).filter((n) => n.status === "active");
  const accounts = host === "local" ? [] : (machines.find((n) => n.name === host)?.accounts ?? []);
  const remote = host !== "local";
  // where the folder goes on that machine (its account's workspace root, as its broker reports it)
  const root = useQuery({ queryKey: ["new-project-folder", host, account], enabled: open && remote && (accounts.length < 2 || !!account),
    queryFn: () => api<{ root: string; account: string }>(`/api/projects/new/folder?host=${encodeURIComponent(host)}${account ? `&account=${encodeURIComponent(account)}` : ""}`) });
  const slug = projectSlug(name);
  const pick = (next: Source) => { setSource(next); setTrust(next === "empty"); };
  const create = useMutation({
    mutationFn: async () => {
      const project = createdProject ?? await (source === "github"
      ? post<Project>("/api/github/repos", { name: slug, description, confirm: confirmGithub })
      : post<Project>("/api/projects/new", { name: name.trim(), description, source, url: source === "clone" ? url.trim() : null, trust,
          host, account: remote && account ? account : null }));
      setCreatedProject(project);
      if (documentFiles.length) await uploadDocuments(project.id, documentFiles);
      return project;
    },
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      setName(""); setUrl(""); setDescription(""); setConfirmGithub(false);
      setDocumentFiles([]); setCreatedProject(null);
      onCreated?.(project);
      onClose();
    },
  });
  // A repository URL or owner/name fills in the name, as people paste the URL first.
  const pasted = (value: string) => {
    setUrl(value);
    const last = value.trim().replace(/\.git\/?$|\/$/, "").split(/[/:]/).pop() ?? "";
    if (!name.trim() && last) setName(last);
  };
  const ready = !!slug && (source !== "clone" || !!url.trim()) && (source !== "github" || confirmGithub)
    && (!remote || accounts.length < 2 || !!account);
  const base = remote ? (root.data?.root ?? `${host}'s workspaces folder`) : (instance.workspaces_root ?? "your workspaces folder");
  const folder = `${base}/${slug || "<name>"}`;
  const runsAs = remote ? (account || root.data?.account || accounts[0] || "its agent account") : instance.agent_user;
  return (
    <Dialog open={open} onClose={onClose} dismissible={false} title="New project"
      description={desktopBuild ? "A local folder for agent sessions, created in the separate agent account." : `A folder for agent sessions, made on ${remote ? host : instance.host} for you. Nothing to set up there.`}
      footer={<>
        <ErrorNote error={create.error} />
        {createdProject && <p className="text-xs text-muted">Project created. Retry the upload, or open its Brief page to choose files again.</p>}
        <Button variant="ghost" disabled={create.isPending} onClick={() => {
          if (createdProject) { queryClient.invalidateQueries({ queryKey: ["projects"] }); onCreated?.(createdProject); setCreatedProject(null); setDocumentFiles([]); }
          onClose();
        }}>{createdProject ? "Open created project" : "Cancel"}</Button>
        <Button variant="primary" disabled={!ready || create.isPending} onClick={() => create.mutate()}>
          <FolderPlus size={14} aria-hidden="true" /> {create.isPending ? "Working…" : createdProject ? "Retry document upload" : source === "clone" ? "Clone and add" : source === "github" ? "Create repository" : "Create project"}
        </Button>
      </>}>
      <div className="space-y-4">
        {machines.length > 0 && (
          <Field label="Machine" hint="Where the folder is made and its sessions run.">
            <Select value={host} onChange={(e) => { setHost(e.target.value); setAccount(""); if (e.target.value !== "local" && source === "github") pick("empty"); }}>
              <option value="local">{instance.host} · main</option>
              {machines.map((n) => <option key={n.id} value={n.name} disabled={!n.connected}>{n.name}{n.connected ? "" : " (offline)"}</option>)}
            </Select>
          </Field>
        )}
        {accounts.length > 1 && (
          <Field label="Agent account" hint={`Which of ${host}'s agent accounts owns the folder and runs its sessions.`}>
            <Select value={account} onChange={(e) => setAccount(e.target.value)}>
              <option value="">Choose…</option>
              {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
            </Select>
          </Field>
        )}
        <Segmented label="Start from" value={source} onChange={pick} options={[
          { id: "empty", label: "Empty folder" }, { id: "clone", label: "Clone repository" },
          ...(github && !remote ? [{ id: "github" as const, label: "New GitHub repo" }] : [])]} />
        {source === "clone" && (
          <Field label="Repository" hint={`An HTTPS or SSH URL, or owner/name on GitHub. It's cloned as ${runsAs}${remote ? ` on ${host}` : ""}, with that account's own SSH key or GitHub sign-in (Providers${remote ? `, with ${host} picked` : ""}).`}>
            <Input value={url} onChange={(e) => pasted(e.target.value)} placeholder="https://git.example.com/team/app.git" autoFocus spellCheck={false} />
          </Field>
        )}
        <Field label="Name" hint={<>Folder{remote ? ` on ${host}` : ""}: <span className="font-mono [overflow-wrap:anywhere]">{folder}</span>{root.error ? <span className="text-wait"> ({host} didn't answer; it's made under its account's workspace root)</span> : null}</>}>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Hello app" maxLength={source === "github" ? 63 : 200} autoFocus={source !== "clone"} />
        </Field>
        <Field label="What do you want to build? (starts your project brief)"
          hint={`Optional. Saved as project notes and brief version 1, ready to pin to a team.${source === "github" ? " The first 300 characters also become the GitHub repository description." : ""}`}>
          <Textarea name="project-description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} rows={3} />
        </Field>
        {instance.features?.orchestrator !== false && <BriefDocuments files={documentFiles} onFiles={setDocumentFiles} />}
        {source === "github" ? (
          <>
            <label className="flex items-start gap-2 rounded-md border border-wait/30 bg-wait/10 p-3 text-xs text-wait">
              <input type="checkbox" checked={confirmGithub} onChange={(e) => setConfirmGithub(e.target.checked)} className="mt-0.5" />
              <span>Create a <b>private</b> repository <span className="font-mono">{slug || "<name>"}</span> on github.com under Rynhult's GitHub account, protect <span className="font-mono">main</span> (changes land through pull requests), clone it as {instance.agent_user} and trust it for agent sessions.</span>
            </label>
          </>
        ) : (
          <label className="flex items-start gap-2.5 text-[13px]">
            <input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} className="mt-0.5 size-3.5 accent-[var(--accent)]" />
            <span><span className="font-medium">Trust it for agent sessions</span>
              <span className="block text-xs text-muted">{source === "clone"
                ? "Off for a clone: trust lets agents run with the repository's own config and hooks."
                : "Agents can read, edit and run files in this folder, within the permissions you pick for each session. Without it, sessions can't start here yet."}</span></span>
          </label>
        )}
      </div>
    </Dialog>
  );
}
