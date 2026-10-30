import { DeliveryHistory, SessionDeliveries } from '../components/SessionDeliveries';
import { Feature, useFeatures } from "../components/Feature";
import { PreviewBadge } from "../components/Containers";
import { CopyBlock } from "../components/InstallSteps";
import clsx from "clsx";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { SessionAutomation } from "../components/SessionAutomation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, BookMarked, ChevronDown, Code2,  Download, ExternalLink, FolderUp, GitBranch, GitPullRequest, Pencil, TerminalSquare } from "lucide-react";
import { api, isNotFound, post } from "../lib/api";
import { stamp, workspaceName, workspaceTopic } from "../lib/format";
import type { AccEvent, AttentionItem, GithubSnapshot, Project, SessionDetail } from "../lib/types";
import { announce, Badge, Button, Card, Dialog, Empty, ErrorNote, Field, IconButton, Input, LoadingRows, Page, PageHeader, Popover, QueryState, Select, Skeleton, TabPanel, Tabs, useDocumentTitle } from "../components/ui";
import { isLive, NowDoing, permissionWords, ProviderBadge, SessionActions, sessionMachineName, sessionState, StateBadge } from "../components/session-bits";
import { Artifacts, useArtifacts } from "../components/Artifacts";
import { DocList, DocViewer, type KnowledgeView, KnowledgePanel, useKnowledge } from "../components/Knowledge";
import { Fragment, lazy, type ReactNode, Suspense, useEffect, useRef, useState } from "react";
import { providerLabel, useInstance, useProviderRegistry, useRole } from "../lib/queries";
import { eventWords, notificationWords, permissionSummary, sourceWords } from "../lib/words";
import { RiskLabel } from "../components/RiskLabel";
import { useDismissableLayer } from "../components/keyboard";
import { lazyImport } from "../lib/lazyImport";

const Files = lazy(() => lazyImport(() => import("../components/Files")).then((m) => ({ default: m.Files })));
const Changes = lazy(() => lazyImport(() => import("../components/Changes")).then((m) => ({ default: m.Changes })));
const Terminal = lazy(() => lazyImport(() => import("../components/Terminal")).then((m) => ({ default: m.Terminal })));
const Preview = lazy(() => lazyImport(() => import("../components/Preview")).then((m) => ({ default: m.Preview })));
const PullRequestPanel = lazy(() => lazyImport(() => import("../components/PullRequest")).then((m) => ({ default: m.PullRequestPanel })));
const TeamPanel = lazy(() => lazyImport(() => import("../components/TeamPanel")).then((m) => ({ default: m.TeamPanel })));

function usePendingRisk(session: SessionDetail) {
  return useQuery({
    queryKey: ["attention", "pending", session.id],
    queryFn: () => api<AttentionItem[]>(`/api/attention?scope=pending&session_id=${session.id}`),
    refetchInterval: 3000,
  });
}

/** On a tab other than Terminal: the session is waiting, with the way back to the terminal. On the Terminal tab the
    header's state and risk label say it instead, so the terminal stays high on the page (UX review R9). */
function NeedsYou({ session, onTerminal }: { session: SessionDetail; onTerminal: () => void }) {
  const pending = usePendingRisk(session);
  return (
    <section aria-label="Needs you" className="max-md:hidden">
      <Card className="space-y-2 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[13px] font-medium text-wait">Needs you</p>
          <Button size="sm" onClick={onTerminal}><TerminalSquare size={14} aria-hidden="true" /> Go to terminal</Button>
        </div>
        <p className="text-xs text-muted">Answer in the terminal. Labels are advisory only; nothing is approved automatically.</p>
        <QueryState query={pending}>{(items) => {
          const item = items.find((i) => i.kind === "needs_input");
          return item ? <RiskLabel risk={item.risk} /> : null;
        }}</QueryState>
      </Card>
    </section>
  );
}

/** Desktop, Terminal tab: the advisory label beside the header's state, instead of a banner above the terminal. */
function HeaderRisk({ session }: { session: SessionDetail }) {
  const item = usePendingRisk(session).data?.find((i) => i.kind === "needs_input");
  return item?.risk ? <span className="text-[13px] font-normal"><RiskLabel risk={item.risk} /></span> : null;
}

/** Phones: the same advisory label in one line under the session's name, so the terminal stays near the top. */
function NeedsYouLine({ session }: { session: SessionDetail }) {
  const item = usePendingRisk(session).data?.find((i) => i.kind === "needs_input");
  return (
    <section aria-label="Needs you" className="mt-1 text-xs text-muted">
      {item?.risk ? <RiskLabel risk={item.risk} /> : "Answer in the terminal below. Labels are advisory only."}
    </section>
  );
}

const TABS = ["Terminal", "Files", "Preview", "Changes", "Pull request", "Artifacts", "Activity", "Handoffs", "Setup", "Team"] as const;
type Tab = (typeof TABS)[number];
const hasPullTab = (session: SessionDetail) => !!session.repo && !session.standalone;
const tabLabel = (t: Tab, standalone: boolean) => (t === "Handoffs" && standalone ? "Findings" : t);

const stateWords = (raw: string) => sessionState({ observed_state: raw as SessionDetail["observed_state"] }).label;

function Activity({ id }: { id: string }) {
  const registry = useProviderRegistry();
  const providerName = (provider: string) => providerLabel(provider, registry.byId(provider));
  const events = useQuery({ queryKey: ["events", id], queryFn: () => api<AccEvent[]>(`/api/sessions/${id}/events?limit=300`), refetchInterval: 3000 });
  return (
    <QueryState query={events} isEmpty={(data) => !data.length} empty={<Empty>No events yet.</Empty>}>{(data) => (
    <Card className="overflow-hidden">
      <div className="max-h-full divide-y divide-line overflow-y-auto">
        {[...data].reverse().map((e) => (
          <div key={e.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1 px-4 py-2 text-[13px] sm:grid-cols-[9.5rem_8rem_minmax(0,1fr)] sm:gap-3">
            <span className="font-mono text-[11px] text-faint">{stamp(e.ts)}</span>
            <span className="truncate text-muted" title={e.source}>{sourceWords(e.source, providerName)}</span>
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="font-medium" title={e.kind}>{eventWords(e, stateWords)}</span>
              <span className="block text-xs text-faint sm:ml-2 sm:inline">
                {e.payload.notification_type ? notificationWords(String(e.payload.notification_type))
                  : String(e.payload.reason ?? e.payload.message ?? e.payload.error ?? "")}
              </span>
              {!e.verified && <Badge tone="wait" className="ml-2" title="Not confirmed by the agent's hooks">guessed</Badge>}
            </span>
          </div>
        ))}
      </div>
    </Card>
    )}</QueryState>
  );
}

const handoffsFor = (view: KnowledgeView, sessionId: string) =>
  (view.docs.handoffs ?? []).filter((doc) => doc.path.includes(sessionId));

function SessionDeliverables({ session, onNavigate }: { session: SessionDetail; onNavigate?: () => void }) {
  const knowledge = useKnowledge(session.project_id ?? undefined, session.id);
  const artifacts = useArtifacts(session);
  const handoffs = knowledge.isSuccess ? handoffsFor(knowledge.data, session.id).length : null;
  const files = artifacts.isSuccess ? artifacts.data.files.length : null;
  const handoffLabel = knowledge.isError ? "Handoffs unavailable" : handoffs === null ? "Checking handoffs…"
    : handoffs ? `${handoffs} handoff${handoffs === 1 ? "" : "s"}` : "No handoff yet";
  const artifactLabel = artifacts.isError ? "Artifacts unavailable" : files === null ? "Checking artifacts…"
    : files ? `${files}${artifacts.data?.truncated ? "+" : ""} artifact${files === 1 && !artifacts.data?.truncated ? "" : "s"}` : "No artifacts yet";
  const link = "inline-flex min-h-6 pointer-coarse:min-h-11 items-center hover:text-fg underline underline-offset-2 pointer-coarse:min-h-11";
  return <span role="group" aria-label="Session deliverables" className="inline-flex flex-wrap items-center gap-x-3">
    <Link to="?tab=handoffs" replace onClick={onNavigate} className={clsx(link, knowledge.isError && "text-wait")}>{handoffLabel}</Link>
    <Link to="?tab=artifacts" replace onClick={onNavigate} className={clsx(link, artifacts.isError && "text-wait")}>{artifactLabel}</Link>
  </span>;
}

/** Handoffs this session wrote, read from its own folder (worktree, shared checkout or helper worktree). A failed or
    unfinished read is never shown as "no handoff" (UX review SE-2, SE-3). */
function SessionHandoffs({ session }: { session: SessionDetail }) {
  const knowledge = useKnowledge(session.project_id ?? undefined, session.id);
  const [open, setOpen] = useState<string | null>(null);
  const mine = (view: KnowledgeView) => handoffsFor(view, session.id);
  return (
    <Card>
      <QueryState query={knowledge} loading={<LoadingRows rows={2} />} isEmpty={(view) => !mine(view).length} errorTitle="Couldn't read this session's handoff notes."
        empty={<Empty icon={BookMarked}>No handoff note yet. The agent writes one before it stops: what changed, tests run, open questions.
          <span className="mt-1 block text-xs text-faint" title={`docs/handoffs/YYYY-MM-DD-${session.id}-<topic>.md`}>Looked in {
            knowledge.data?.folder === "worktree" ? "the session's worktree" : knowledge.data?.folder === "workspace" ? "the workspace folder" : "the project's checkout"}.</span></Empty>}>
        {(view) => <DocList entries={mine(view)} onOpen={setOpen} />}
      </QueryState>
      <DocViewer source={{ sessionId: session.id }} path={open} onClose={() => setOpen(null)} />
    </Card>
  );
}

const RUN_WORDS: Record<string, string> = { launch: "Started", resume: "Resumed", explanation: "Started to explain its changes", adopted: "Picked up after a Rynhult restart" };

/** Setup (was "Launch spec", UX review SE-4): what this session was given, in words first; the exact inputs and the
    generated files follow for anyone who needs them. */
function Setup({ session }: { session: SessionDetail }) {
  const registry = useProviderRegistry();
  const inputs = session.launch_spec.inputs as Record<string, unknown>;
  const list = (key: string) => (Array.isArray(inputs[key]) ? inputs[key] as string[] : []);
  const provider = String(inputs.provider ?? session.provider);
  const permissions = permissionSummary(provider, inputs);
  const { host } = useInstance();
  const fileNames = Object.keys(session.launch_spec.files);
  const [filter, setFilter] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | undefined>(() => fileNames[0]);
  const filteredFiles = fileNames.filter(name => name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const activeFile = selectedFile !== undefined && filteredFiles.includes(selectedFile) ? selectedFile : filteredFiles[0];
  const items: [string, ReactNode][] = [
    ["Agent", <>{providerLabel(provider, registry.byId(provider))} · {inputs.model ? String(inputs.model) : "default model"}{inputs.effort ? ` · ${inputs.effort} effort` : ""}</>],
    ["Permissions", permissions],
    ["Profile", inputs.profile ? String(inputs.profile) : "None"],
    ["Skills", list("skills").length ? list("skills").join(", ") : "None"],
    ...(list("agents").length ? [["Subagents", list("agents").join(", ")] as [string, ReactNode]] : []),
    ["MCP servers", list("mcps").length ? list("mcps").join(", ") : "None"],
    ["Folder", <><span className="font-mono">{session.cwd}</span>{session.worktree_branch && <> · its own {session.container ? "private checkout" : "worktree"} on <span className="font-mono">{session.worktree_branch}</span></>}</>],
    ...(session.container ? [["Runs in", <span>A container <PreviewBadge />, with its account's container limits</span>] as [string, ReactNode]] : []),
    ["Machine", sessionMachineName(session, host)],
    ["Runs as", <span className="font-mono">{session.run_as}</span>],
  ];
  return (
    <div className="space-y-4">
      <Card className="min-w-0 p-4">
        <h3 className="mb-3 text-[13px] font-semibold">How this session was set up</h3>
        <dl className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-[9rem_minmax(0,1fr)]">
          {items.map(([label, value]) => <Fragment key={label}><dt className="text-muted">{label}</dt><dd className="min-w-0 break-words text-fg">{value}</dd></Fragment>)}
          <dt className="text-muted">History</dt>
          <dd className="min-w-0 space-y-1">
            {session.runs.map((r) => (
              <div key={r.id} className="flex flex-wrap gap-x-3 text-muted">
                <span className="text-fg" title={`run #${r.id} · ${r.reason}`}>{RUN_WORDS[r.reason] ?? r.reason}</span>
                <span>{stamp(r.started_at)}</span>
                {r.ended_at && <span>ended {r.exit_signal ? `by signal ${r.exit_signal}` : r.exit_status === 0 ? "normally" : `with status ${r.exit_status ?? "unknown"}`}{r.end_reason ? ` · ${r.end_reason}` : ""}</span>}
              </div>
            ))}
          </dd>
        </dl>
      </Card>
      <Card className="min-w-0 p-4">
        <details>
          <summary className="min-h-8 cursor-pointer break-words text-[13px] font-semibold pointer-coarse:min-h-11">
            Exact inputs <span className="ml-2 font-mono text-[11px] font-normal text-faint" title="Digest of the inputs">{session.launch_spec.digest.slice(0, 12)}</span>
          </summary>
          <div className="mt-3 min-w-0">
            <CopyBlock text={JSON.stringify(session.launch_spec.inputs, null, 2)} label="Copy launch inputs" stacked />
          </div>
        </details>
      </Card>
      <Card className="min-w-0 p-4">
        <details>
          <summary className="min-h-8 cursor-pointer text-[13px] font-semibold pointer-coarse:min-h-11">Generated files ({fileNames.length})</summary>
          <div className="mt-3 min-w-0 space-y-3">
            {!!fileNames.length && <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <Field label="Filter generated files">
                <Input value={filter} onChange={event => setFilter(event.target.value)} type="search" autoComplete="off" spellCheck={false} />
              </Field>
              {!!filteredFiles.length && <Field label="Generated file">
                <Select value={activeFile ?? ""} onChange={event => setSelectedFile(event.target.value)} className="w-full min-w-0">
                  {filteredFiles.map(name => <option key={name} value={name}>{name}</option>)}
                </Select>
              </Field>}
            </div>}
            <p role="status" aria-label="Generated files results" className="text-xs text-muted">
              {!fileNames.length ? "No generated files." : !filteredFiles.length ? "No matching files." : `${filteredFiles.length} of ${fileNames.length} files`}
            </p>
            {activeFile !== undefined && <CopyBlock key={activeFile} text={session.launch_spec.files[activeFile]} label={`Copy ${activeFile}`} stacked />}
          </div>
        </details>
      </Card>
    </div>
  );
}

function PromoteButton({ session }: { session: SessionDetail }) {
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

const ciTone = (run: { status: string; conclusion: string | null }) =>
  run.status !== "completed" ? "accent" : run.conclusion === "success" ? "ok"
    : ["failure", "timed_out", "startup_failure"].includes(run.conclusion ?? "") ? "bad" : "neutral";

/** The branch the session's folder is on and, from the project's GitHub view, the open pull request from it and its CI. */
function BranchAndPull({ session }: { session: SessionDetail }) {
  const branch = session.changes?.branch ?? session.worktree_branch;
  const github = useQuery({
    queryKey: ["github", String(session.project_id)], queryFn: () => api<GithubSnapshot>(`/api/projects/${session.project_id}/github`),
    refetchInterval: 60000, enabled: !!branch && !session.standalone,
  });
  if (!branch) return null;
  const pull = github.data?.pulls?.find((p) => p.head === branch);
  const run = github.data?.runs?.find((r) => r.branch === branch);  // newest first
  return (
    <>
      {session.multirun_id && <Feature name="multirun"><Link to={`/multiruns/${session.multirun_id}`} className="text-accent hover:underline underline underline-offset-2">Multi-run comparison</Link></Feature>}
      <span className="inline-flex items-center gap-1.5" title={session.worktree ?? undefined}>
        {session.worktree && <Badge tone="info">worktree</Badge>}
        <GitBranch size={12} aria-hidden="true" /><span className="font-mono">{branch}</span>
      </span>
      {pull && (
        <a href={pull.url} target="_blank" rel="noreferrer" title={pull.title} className="inline-flex pointer-coarse:min-h-11 items-center gap-1 hover:text-fg underline underline-offset-2">
          <GitPullRequest size={12} aria-hidden="true" /> #{pull.number}{pull.draft ? " (draft)" : ""}
        </a>
      )}
      {pull && run && (
        <a href={run.url} target="_blank" rel="noreferrer" title={run.name} className="inline-flex min-h-6 pointer-coarse:min-h-11 items-center gap-1 underline underline-offset-2">
          <Badge tone={ciTone(run)}>CI {run.status === "completed" ? (run.conclusion ?? "done") : "running"}</Badge><ExternalLink size={12} aria-hidden="true" />
        </a>
      )}
    </>
  );
}

/** VS Code Remote-SSH into the session's folder (its worktree if it has one): a link, and the command to paste. */
function OpenInEditor({ editor }: { editor: NonNullable<SessionDetail["editor"]> }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useDismissableLayer({ open, root: box, dismissOnOutside: true, onDismiss: () => setOpen(false) });
  const quote = (s: string) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`);
  const command = `code --remote ssh-remote+${editor.ssh_host} ${quote(editor.path)}`;
  const link = `vscode://vscode-remote/ssh-remote+${encodeURIComponent(editor.ssh_host)}${editor.path.split("/").map(encodeURIComponent).join("/")}`;
  return (
    <div ref={box} className="relative">
      <Button aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}><Code2 size={14} aria-hidden="true" /> Open in editor</Button>
      {open && (
        <div className="absolute right-0 z-20 mt-1.5 w-[22rem] max-w-[calc(100vw-2rem)] space-y-3 rounded-[10px] border border-line-strong bg-panel p-3 shadow-lg">
          <a href={link} onClick={() => setOpen(false)}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium text-fg hover:bg-panel-2">
            <ExternalLink size={14} aria-hidden="true" /> VS Code (Remote-SSH)
          </a>
          <div className="space-y-1.5 px-2">
            <p className="text-xs text-muted">Or from a terminal on your computer:</p>
            <CopyBlock text={command} label="Copy command" />
            <p className="text-[11px] text-faint">Needs SSH access to {editor.ssh_host} with an account that can open this folder.</p>
          </div>
        </div>
      )}
    </div>
  );
}

function RenameButton({ session }: { session: SessionDetail }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(session.name);
  const rename = useMutation({
    mutationFn: () => post<SessionDetail>(`/api/sessions/${session.id}/rename`, { name: name.trim() }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["sessions"] }); setOpen(false); },
  });
  return (
    <>
      <IconButton icon={Pencil} label="Rename session" onClick={() => { setName(session.name); setOpen(true); }} />
      <Dialog open={open} onClose={() => setOpen(false)} title="Rename session" dismissible={false}
        description="Changes the display name only; the terminal and conversation are unaffected."
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim() || rename.isPending} onClick={() => rename.mutate()}>Rename</Button>
        </>}>
        <div className="min-w-0 space-y-3">
          <Field label="Name"><Input value={name} maxLength={40} onChange={(e) => setName(e.target.value)} autoFocus
            onKeyDown={(e) => e.key === "Enter" && name.trim() && rename.mutate()} /></Field>
          <ErrorNote error={rename.error} />
        </div>
      </Dialog>
    </>
  );
}

/** The Artifacts tab shows only when the agent left some (or when a link opens it); Pull request only for a project with a
 * GitHub repository. */
/** A tab this session can't show (Team without a team, Pull request without a GitHub repository) falls back to Terminal
    (UX review SE-13), so a stale link never opens an empty page. */
const availableTab = (session: SessionDetail, tab: Tab): Tab =>
  (tab === "Team" && session.team_id !== session.id) || (tab === "Pull request" && !hasPullTab(session)) ? "Terminal" : tab;

function SessionTabs({ session, tab, setTab }: { session: SessionDetail; tab: Tab; setTab: (t: Tab) => void }) {
  const feature = useFeatures();
  const artifacts = useArtifacts(session).data?.files.length ?? 0;
  const shown = TABS.filter((t) => (t !== "Preview" || feature("live_preview")) && (t !== "Team" || feature("teams")) && (t !== "Team" || session.team_id === session.id) && (t !== "Artifacts" || artifacts > 0 || tab === "Artifacts") && (t !== "Pull request" || hasPullTab(session)));
  return <Tabs id="session-tabs" label="Session views" tabs={shown.map((t) => ({ id: t, label: tabLabel(t, session.standalone),
    count: t === "Changes" ? session.changes?.files || undefined : t === "Artifacts" ? artifacts || undefined : undefined }))} value={tab} onChange={setTab} className="shrink-0"
    phone={["Terminal", "Changes"]} />;
}

/** Says a state change out loud once (polite), without reading the terminal's stream (UX review SE-9). */
function useAnnounceState(session: SessionDetail | undefined) {
  const label = session ? sessionState(session).label : null;
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (!label || !session) return;
    if (last.current && last.current !== label) announce(`${session.name}: ${label}`);
    last.current = label;
  }, [label, session?.id]);
}

export function SessionPage() {
  const { id = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const tabParam = search.get("tab");
  const requested: Tab = tabParam === "team" ? "Team" : tabParam === "activity" ? "Activity" : ["spec", "automation", "goal"].includes(tabParam ?? "") ? "Setup" : tabParam === "handoffs" ? "Handoffs"
    : tabParam === "files" ? "Files" : tabParam === "preview" ? "Preview" : tabParam === "changes" ? "Changes" : tabParam === "pull" ? "Pull request" : tabParam === "artifacts" ? "Artifacts" : "Terminal";
  const setTab = (t: Tab) =>
    setSearch(t === "Terminal" ? {} : { tab: t === "Activity" ? "activity" : t === "Handoffs" ? "handoffs" : t === "Changes" ? "changes" : t === "Pull request" ? "pull"
      : t === "Preview" ? "preview" : t === "Files" ? "files" : t === "Artifacts" ? "artifacts" : t === "Team" ? "team" : "spec" }, { replace: true });
  const query = useQuery({ queryKey: ["sessions", id], queryFn: () => api<SessionDetail>(`/api/sessions/${id}`), refetchInterval: 3000,
    retry: (count, error) => !isNotFound(error) && count < 3 });
  const session = query.data;
  const { host } = useInstance();
  const feature = useFeatures();
  const [editingAutomation, setEditingAutomation] = useState(false);
  useDocumentTitle(session?.name);
  useAnnounceState(session);
  if (!session) return <Page>{isNotFound(query.error) ? (
    <Card><Empty icon={TerminalSquare} action={<Link to="/sessions" className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2 underline underline-offset-2"><ArrowLeft size={14} aria-hidden="true" /> Back to sessions</Link>}>
      <span className="block text-[15px] font-semibold text-fg">There's no session here</span>
      It was archived and removed, the link is mistyped, or it belongs to someone else.
    </Empty></Card>
  ) : query.error ? <ErrorNote error={query.error} title="Couldn't load this session." onRetry={query.refetch} /> : <Skeleton className="h-64" />}</Page>;
  const tab = availableTab(session, requested);
  const waiting = session.observed_state === "waiting_input";
  const automation = session.management !== "external";
  const projectLink = session.standalone
    ? <span className="inline-flex items-center gap-1.5"><Badge tone="info">local workspace</Badge><span className="font-mono">{session.workdir?.split("/").pop()}</span></span>
    // One line, truncated, never broken mid-name ("demo-" / "web") on a phone (UX review R10).
    : session.project_id === null ? <span>{session.project} · deleted project</span> : <Link to={`/projects/${session.project_id}`} title={session.project} className="inline-flex min-h-6 pointer-coarse:min-h-11 min-w-0 max-w-[65%] shrink-0 items-center whitespace-nowrap hover:text-fg underline underline-offset-2"><span className="truncate">{session.project}</span></Link>;
  const meta = (onNavigate?: () => void) => <>
    <BranchAndPull session={session} />
    <SessionDeliverables session={session} onNavigate={onNavigate} />
    <span className="font-mono">{session.tmux_name}</span>
    {session.native_id && (
      <CopyBlock text={session.native_id} label="Copy conversation ID" />
    )}
    {session.permission_mode && session.provider === "claude" && <span title={`permission mode: ${session.permission_mode}`}>{permissionWords(session.permission_mode)}</span>}
  </>;
  const exportLink = (
    <a href={`/api/sessions/${session.id}/export`} download title="Download the session as Markdown: details, conversation, handoffs and launch command"
      className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-line-strong bg-panel-2 px-3 text-[13px] font-medium text-fg transition-colors hover:bg-panel-3 underline underline-offset-2">
      <Download size={14} aria-hidden="true" /> Export</a>
  );
  return (
    <div className="flex h-full flex-col gap-3 px-4 pb-4 pt-3 md:gap-4 md:px-8 md:pb-6 md:pt-5">
      <div className="session-heading grid min-w-0 shrink-0 grid-cols-[minmax(0,1fr)_auto] gap-x-2 md:grid-cols-[minmax(0,1fr)] md:gap-x-0">
      <div className="col-span-full row-start-1 max-md:hidden">
        <PageHeader
          title={<span className="flex flex-wrap items-center gap-2.5">{session.name}<RenameButton session={session} /><ProviderBadge provider={session.provider} model={session.model} /><StateBadge session={session} />{waiting && tab === "Terminal" ? <HeaderRisk session={session} />
            : <NowDoing session={session} className="max-w-[28rem] truncate text-[13px] font-normal text-muted" />}</span>}
          meta={<>{projectLink}{meta()}</>}
          actions={<>{exportLink}{session.editor && <OpenInEditor editor={session.editor} />}{session.standalone && <PromoteButton session={session} />}<SessionActions session={session} /></>} />
      </div>
      {/* Phones: name, state and project in one line; everything else under Details, so the terminal starts high
          on the screen (UX review SE-6). Interrupt and Stop sit behind "⋯" on touch (OV-4). */}
      <header className="contents md:hidden">
        <div className="contents">
          <h1 className="col-start-1 row-start-1 flex min-w-0 items-center gap-2 text-[15px] font-semibold leading-tight">
            <span className="truncate">{session.name}</span><StateBadge session={session} />
          </h1>
          <div className="phone-session-meta col-start-1 row-start-2 flex min-w-0 items-center gap-2 text-xs text-muted">{projectLink}
            <NowDoing session={session} className="truncate" /></div>
          {session.observed_state === "waiting_input" && <div className="col-start-1 row-start-3"><NeedsYouLine session={session} /></div>}
        </div>
        <div className="col-start-2 row-start-1 row-span-3 flex items-center gap-2">
        <Popover label="Session details" wide trigger={<>Details <ChevronDown size={13} aria-hidden="true" /></>}>
          {(close) => (
            <div className="space-y-3 p-1.5">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted">
                <ProviderBadge provider={session.provider} model={session.model} />{meta(close)}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <RenameButton session={session} />{exportLink}
                {session.editor && <OpenInEditor editor={session.editor} />}{session.standalone && <PromoteButton session={session} />}
              </div>
            </div>
          )}
        </Popover>
        <SessionActions session={session} compact />
        </div>
      </header>
        <div className="pointer-events-none col-start-1 row-start-2 min-w-0 md:pointer-events-auto md:mt-4 md:h-8 md:pointer-coarse:h-11">
          <SessionDeliveries key={session.id} id={session.id}
            onDetails={() => setSearch({ tab: "activity", delivery: "details" }, { replace: true })} />
        </div>
        <div className="col-span-full row-start-4 mt-3 md:mt-0"><SessionTabs session={session} tab={tab} setTab={setTab} /></div>
      </div>
      <TabPanel id="session-tabs" value={tab} className="min-h-[420px] flex-1">
        {session.machine_offline && <p className="text-sm text-muted">Machine offline. Sessions check in when the machine returns.</p>}
        {!session.machine_offline && session.last_error && <ErrorNote error={session.last_error} />}
        {waiting && tab !== "Terminal" && <NeedsYou session={session} onTerminal={() => setTab("Terminal")} />}
        <Suspense fallback={<div role="status" className="h-full min-h-[420px]">
          <Skeleton className="h-full min-h-[420px]" /><span className="sr-only">Loading tab…</span>
        </div>}>
        {tab === "Terminal" && session.management === "external" && (
          <Card className="p-6">
            <div className="flex flex-col items-start gap-4 md:flex-row md:items-center md:justify-between">
              <div className="max-w-2xl space-y-1.5 text-[13px] text-muted">
                <p className="text-[15px] font-semibold text-fg">Open this session in Claude Remote Control</p>
                <p>It runs as <span className="font-mono text-fg">{session.run_as}</span> and is started by <span className="font-mono text-fg">{session.external_unit}</span>, not by Rynhult. Rynhult shows its status and activity but never types into it, so the dashboard never holds {session.run_as} access. Continue it in your browser or the Claude app instead.</p>
              </div>
              {session.remote_url
                ? <a href={session.remote_url} target="_blank" rel="noreferrer"
                    className="inline-flex h-9 shrink-0 items-center gap-2 rounded-md bg-accent px-4 text-[13px] font-medium text-accent-ink shadow-sm hover:brightness-110 underline underline-offset-2">
                    <ExternalLink size={15} aria-hidden="true" /> Open in Remote Control</a>
                : <span className="text-xs text-wait">No Remote Control link reported yet.</span>}
            </div>
            <p className="mt-5 border-t border-line pt-4 text-xs text-faint">From a shell on {host}: <span className="font-mono text-muted">sudo tmux -L acc-root attach -t acc-root</span></p>
          </Card>
        )}
        {tab === "Terminal" && session.management !== "external" && (session.machine_offline
          ? <Card><Empty icon={TerminalSquare}>Machine offline. This view returns when the machine checks in.</Empty></Card> : isLive(session) || session.observed_state === "exited"
          ? <Terminal key={session.id} sessionId={session.id} live={isLive(session)} />
          : <Card><Empty icon={TerminalSquare}>This session isn't running. Resume it to open its terminal.</Empty></Card>)}
        {tab === "Team" && session.project_id !== null && !session.machine_offline && session.team_id === session.id && <Feature name="teams"><TeamPanel id={session.id} projectId={session.project_id} /></Feature>}
        {feature("live_preview") && tab === "Preview" && session.project_id !== null && !session.machine_offline && <div className="grid min-w-0 gap-4 xl:grid-cols-2">
          <Preview key={session.id} projectId={session.project_id} projectName={session.project}
            folder={session.worktree ?? session.cwd} branch={session.changes?.branch ?? session.worktree_branch} sessionId={session.id} />
          <div className="min-w-0">{session.management !== "external" && (isLive(session) || session.observed_state === "exited")
            ? <Terminal key={session.id} sessionId={session.id} live={isLive(session)} /> : <Activity id={session.id} />}</div>
        </div>}
        {tab === "Activity" && <div className="space-y-4"><DeliveryHistory id={session.id} focus={search.get("delivery") === "details"} /><Activity id={session.id} /></div>}
        {session.machine_offline && ["Files", "Changes", "Artifacts", "Preview", "Handoffs", "Team"].includes(tab) && <Card><Empty>Machine offline. This view returns when the machine checks in.</Empty></Card>}
        {tab === "Files" && !session.machine_offline && <Files kind="session" scopeKey={session.id} />}
        {tab === "Changes" && !session.machine_offline && <Changes session={session} />}
        {tab === "Pull request" && hasPullTab(session) && <PullRequestPanel session={session} />}
        {tab === "Artifacts" && !session.machine_offline && <Artifacts session={session} />}
        {tab === "Setup" && <div className="space-y-4">
          {feature("automation") && automation && <Feature name="automation">
            <SessionAutomation key={session.id} session={session} editing={editingAutomation} onEditing={setEditingAutomation}
              disabledReason={session.launch_spec.inputs.endpoint ? "Goal and auto-resume need the provider plan, not a model endpoint." : undefined} />
          </Feature>}
          <Setup key={session.id} session={session} />
        </div>}
        {tab === "Handoffs" && session.project_id !== null && !session.machine_offline && (session.standalone
          ? <KnowledgePanel projectId={session.project_id} sessionId={session.id} title="Findings" />
          : <SessionHandoffs session={session} />)}
        </Suspense>
      </TabPanel>
    </div>
  );
}
