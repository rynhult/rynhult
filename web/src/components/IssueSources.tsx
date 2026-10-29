import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleDot, ExternalLink, Link2, Rocket } from "lucide-react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import type { IssueConnections, IssueRef, Project, SourceIssue } from "../lib/types";
import { useIssueServers } from "./IssueServers";
import type { VaultKey } from "./Keys";
import { useNewSession } from "./newSession";
import { Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, Input, QueryState, Select } from "./ui";

export const useIssueConnections = (projectId?: number, enabled = true) => useQuery({
  queryKey: ["issue-sources", projectId],
  queryFn: () => api<IssueConnections>(`/api/projects/${projectId}/issue-sources`),
  enabled: enabled && !!projectId,
});

export function SourceOptions({ config }: { config?: IssueConnections }) {
  return <>
    {config?.gitlab && <><option value="gitlab:issue">GitLab issues</option><option value="gitlab:merge_request">GitLab merge requests</option></>}
    {config?.gitea && <><option value="gitea:issue">Gitea / Forgejo issues</option><option value="gitea:pull_request">Gitea / Forgejo pull requests</option></>}
    {config?.linear && <option value="linear:issue">Linear issues</option>}
  </>;
}

export function IssueList({ projectId, source, kind, onSelect, selecting = false }: {
  projectId: number; source: IssueRef["source"]; kind: IssueRef["kind"];
  onSelect?: (issue: SourceIssue) => void; selecting?: boolean;
}) {
  const [state, setState] = useState("open");
  const [draft, setDraft] = useState({ label: "", assignee: "" });
  const [filters, setFilters] = useState(draft);
  const [pages, setPages] = useState<string[]>([]);
  const cursor = pages.at(-1);
  const params = new URLSearchParams({ kind, state, ...filters, ...(cursor ? { cursor } : {}) });
  const query = useQuery({
    queryKey: ["source-issues", projectId, source, params.toString()],
    queryFn: () => api<{ items: SourceIssue[]; next_cursor: string | null; fetched_at: number }>(
      `/api/projects/${projectId}/issue-sources/${source}?${params}`),
    staleTime: 30_000, retry: false,
  });
  return <div className="space-y-3 p-4">
    <form className="grid items-end gap-2 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); setFilters(draft); setPages([]); }}>
      <Field label="Issue state"><Select value={state} onChange={(e) => { setState(e.target.value); setPages([]); }}>
        <option value="open">Open</option><option value="closed">Closed</option>
        {kind === "merge_request" && <option value="merged">Merged</option>}<option value="all">All</option>
      </Select></Field>
      <Field label="Issue label"><Input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })}
        placeholder="Label name…" maxLength={100} autoComplete="off" /></Field>
      <Field label="Issue assignee" hint={source === "linear" ? "Email address" : "Git username"}>
        <Input value={draft.assignee} onChange={(e) => setDraft({ ...draft, assignee: e.target.value })}
          placeholder={source === "linear" ? "alex@example.com…" : "alex…"} maxLength={100} autoComplete="off" spellCheck={false} />
      </Field>
      <Button type="submit">Apply filters</Button>
    </form>
    <QueryState query={query} isEmpty={(data) => !data.items.length} empty={<Empty>No matching issues.</Empty>}>
      {(data) => <div className="divide-y divide-line">
        {data.items.map((issue) => <div key={issue.id} className="flex flex-wrap items-center gap-2 py-3">
          <div className="min-w-0 flex-1">
            <a href={issue.url} target="_blank" rel="noreferrer" className="break-words font-medium text-accent hover:underline underline underline-offset-2">
              {issue.identifier} {issue.title}<ExternalLink size={12} className="ml-1 inline" aria-hidden="true" />
            </a>
            <p className="text-xs text-muted">{issue.state}{issue.assignee ? ` · ${issue.assignee}` : ""}</p>
            <div className="mt-1 flex flex-wrap gap-1">{issue.labels.map((label) => <Badge key={label}>{label}</Badge>)}</div>
          </div>
          {onSelect && <Button size="sm" variant="ghost"
            aria-label={selecting ? `Use ${issue.identifier}` : `Start session for ${issue.identifier}`}
            onClick={() => onSelect(issue)}><Rocket size={13} aria-hidden="true" />{selecting ? "Use issue" : "Start session"}</Button>}
        </div>)}
      </div>}
    </QueryState>
    {query.data && <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
      <span className="mr-auto">Checked {ago(new Date(query.data.fetched_at * 1000).toISOString())} · cached up to 30 seconds</span>
      {!!pages.length && <Button size="sm" onClick={() => setPages((p) => p.slice(0, -1))}>Previous page</Button>}
      {query.data.next_cursor && <Button size="sm" onClick={() => setPages((p) => [...p, query.data!.next_cursor!])}>Next page</Button>}
    </div>}
  </div>;
}

function ConnectionDialog({ project, config, onClose }: { project: Project; config: IssueConnections; onClose: () => void }) {
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["project-keys", project.id],
    queryFn: () => api<{ keys: VaultKey[] }>(`/api/projects/${project.id}/keys`) });
  const [remote, setRemote] = useState(config.remote_url ?? project.remote_url ?? "");
  const [gitlabKey, setGitlabKey] = useState(config.gitlab?.key_id ?? "");
  const [base, setBase] = useState(config.gitlab?.base_url ?? "https://gitlab.com");
  const servers = useIssueServers();
  const [gitlabServer, setGitlabServer] = useState(config.gitlab?.server_id ?? "");
  const [giteaServer, setGiteaServer] = useState(config.gitea?.server_id ?? "");
  const [giteaKey, setGiteaKey] = useState(config.gitea?.key_id ?? "");
  const [giteaBase, setGiteaBase] = useState(config.gitea?.base_url ?? "https://gitea.com");
  const [linearKey, setLinearKey] = useState(config.linear?.key_id ?? "");
  const [linear, setLinear] = useState({ team_id: config.linear?.team_id ?? "", project_id: config.linear?.project_id ?? "",
    label_id: config.linear?.label_id ?? "", comment_pr: config.linear?.comment_pr ?? false });
  const save = useMutation({
    mutationFn: () => post(`/api/projects/${project.id}/issue-sources`, { remote_url: remote || null,
      ...(gitlabKey ? { gitlab: { key_id: gitlabKey, ...(gitlabServer ? { server_id: gitlabServer } : { base_url: base }) } } : {}),
      ...(giteaKey ? { gitea: { key_id: giteaKey, ...(giteaServer ? { server_id: giteaServer } : { base_url: giteaBase }) } } : {}),
      ...(linearKey ? { linear: { key_id: linearKey, ...linear, project_id: linear.project_id || null, label_id: linear.label_id || null } } : {}) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["issue-sources", project.id] });
      queryClient.invalidateQueries({ queryKey: ["source-issues", project.id] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      queryClient.invalidateQueries({ queryKey: ["project-keys", project.id] });
      onClose();
    },
  });
  const keySelect = (label: string, value: string, set: (value: string) => void) =>
    <Field label={label}><Select value={value} onChange={(e) => set(e.target.value)}>
      <option value="">Not connected</option>
      {value && !keys.data?.keys.some((key) => key.id === value) && <option value={value}>Unavailable key — select another</option>}
      {keys.data?.keys.map((key) => <option key={key.id} value={key.id}>{key.name}</option>)}
    </Select></Field>;
  const serverSelect = (label: string, value: string, set: (value: string) => void) =>
    <Field label={label} hint="Choose an owner-allowed server for private networks. Public HTTPS hosts need no entry.">
      <Select value={value} onChange={e => set(e.target.value)}>
        <option value="">Public HTTPS server</option>
        {value && !servers.data?.some(server => server.id === value) && <option value={value}>Removed server — choose another</option>}
        {servers.data?.map(server => <option key={server.id} value={server.id}>{server.base_url}</option>)}
      </Select>
    </Field>;
  return <Dialog open title="Issue sources" onClose={onClose} dismissible={false}
    description="Add a token in Settings → Keys, give it to this project, then select it here. Source keys stay on the server, out of agent environments."
    footer={<><ErrorNote error={save.error || keys.error || servers.error} /><Button variant="ghost" onClick={onClose}>Cancel</Button>
      <Button variant="primary" disabled={save.isPending || keys.isPending} onClick={() => save.mutate()}>Save connections</Button></>}>
    <div className="space-y-4">
      <Field label="Repository remote" hint="Git HTTPS or SSH remote; GitLab supports nested groups">
        <Input value={remote} onChange={(e) => setRemote(e.target.value)} placeholder="git@gitlab.example.com:example/demo.git…"
          maxLength={500} autoComplete="off" spellCheck={false} />
      </Field>
      {keySelect("GitLab vault key", gitlabKey, setGitlabKey)}
      {serverSelect("GitLab server", gitlabServer, setGitlabServer)}
      {!gitlabServer && <Field label="GitLab base URL" hint="HTTPS only; must match the remote. Self-hosted path prefixes are supported.">
        <Input type="url" value={base} onChange={(e) => setBase(e.target.value)} autoComplete="off" spellCheck={false} />
      </Field>
      }
      {keySelect("Gitea / Forgejo vault key", giteaKey, setGiteaKey)}
      {serverSelect("Gitea / Forgejo server", giteaServer, setGiteaServer)}
      {!giteaServer && <Field label="Gitea / Forgejo base URL" hint="Public HTTPS only; private servers require an owner-allowed entry.">
        <Input type="url" value={giteaBase} onChange={e => setGiteaBase(e.target.value)} autoComplete="off" spellCheck={false} />
      </Field>}
      {keySelect("Linear vault key", linearKey, setLinearKey)}
      <Field label="Linear team ID"><Input value={linear.team_id} onChange={(e) => setLinear({ ...linear, team_id: e.target.value })} autoComplete="off" /></Field>
      <Field label="Linear project ID (optional)"><Input value={linear.project_id} onChange={(e) => setLinear({ ...linear, project_id: e.target.value })} autoComplete="off" /></Field>
      <Field label="Linear label ID (optional)"><Input value={linear.label_id} onChange={(e) => setLinear({ ...linear, label_id: e.target.value })} autoComplete="off" /></Field>
      <label className="flex items-start gap-2 text-[13px]">
        <input type="checkbox" checked={linear.comment_pr} onChange={(e) => setLinear({ ...linear, comment_pr: e.target.checked })} />
        <span>Comment PR link on Linear
          <span className="block text-xs text-muted">Only when Rynhult observes a PR on this session's own worktree branch. Needs Linear write access. Nothing is assigned or closed.</span>
        </span>
      </label>
    </div>
  </Dialog>;
}

export function ProjectIssueSources({ project, canLaunch }: { project: Project; canLaunch: boolean }) {
  const connections = useIssueConnections(project.id);
  const newSession = useNewSession();
  const [editing, setEditing] = useState(false);
  const [chosen, setChosen] = useState("");
  const config = connections.data;
  const selected = chosen && (chosen.startsWith("gitlab") ? config?.gitlab : chosen.startsWith("gitea") ? config?.gitea : config?.linear)
    ? chosen : config?.gitlab ? "gitlab:issue" : config?.gitea ? "gitea:issue" : config?.linear ? "linear:issue" : "";
  const [source, kind] = selected.split(":") as [IssueRef["source"], IssueRef["kind"]];
  return <>
    <Card>
      <CardHeader title="Issue sources" icon={Link2} subtitle="GitLab, Gitea / Forgejo and Linear · read-only unless you enable the PR-link comment"
        action={<Button size="sm" onClick={() => setEditing(true)} disabled={!config}>
          {config?.gitlab || config?.gitea || config?.linear ? "Edit issue sources" : "Connect issue sources"}</Button>} />
      <ErrorNote error={connections.error} />
      {config?.comments?.map((comment) => <p key={comment.session_id} className="px-4 pb-3 text-xs text-muted" role="status">
        {comment.session}: Linear comment {comment.status}{comment.error ? ` — ${comment.error}` : ""}</p>)}
    </Card>
    {selected && <section aria-label="External issues">
      <Card>
        <CardHeader title="External issues" icon={CircleDot} subtitle="Open an item at its source, or choose it for a session" />
        <div className="px-4 pt-3"><Field label="Issue source"><Select value={selected} onChange={(e) => setChosen(e.target.value)}><SourceOptions config={config} /></Select></Field></div>
        <IssueList key={selected} projectId={project.id} source={source} kind={kind} onSelect={canLaunch ? (issue) =>
          newSession({ mode: "project", project_id: project.id, worktree: true,
            name: `${issue.source}-${issue.id}`.slice(0, 40), issue_source: { source: issue.source, kind: issue.kind, id: issue.id } }) : undefined} />
      </Card>
    </section>}
    {editing && config && <ConnectionDialog project={project} config={config} onClose={() => setEditing(false)} />}
  </>;
}

export function NewSessionIssuePicker({ projectId, selected, onSelect }: {
  projectId: number; selected?: IssueRef; onSelect: (ref?: IssueRef, name?: string) => void;
}) {
  const connections = useIssueConnections(projectId);
  const [chosen, setChosen] = useState(selected ? `${selected.source}:${selected.kind}` : "");
  const [source, kind] = chosen.split(":") as [IssueRef["source"], IssueRef["kind"]];
  return <div className="space-y-2">
    <Field label="Start from"><Select value={chosen} onChange={(e) => { setChosen(e.target.value); onSelect(); }}>
      <option value="">Your own task (type it below)</option><SourceOptions config={connections.data} />
    </Select></Field>
    <ErrorNote error={connections.error} />
    {selected && <div className="flex items-center gap-2 text-xs text-muted">
      <Badge>{selected.source} {selected.kind === "merge_request" ? "MR" : selected.kind === "pull_request" ? "PR" : "issue"} {selected.id}</Badge>
      <Button size="sm" variant="ghost" onClick={() => onSelect()}>Choose another</Button>
    </div>}
    {chosen && !selected && <div className="rounded-lg border border-line">
      <IssueList key={chosen} projectId={projectId} source={source} kind={kind} selecting
        onSelect={(issue) => onSelect({ source: issue.source, kind: issue.kind, id: issue.id }, `${issue.source}-${issue.id}`.slice(0, 40))} />
    </div>}
  </div>;
}
