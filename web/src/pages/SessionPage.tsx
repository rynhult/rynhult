import { SessionActions } from "../components/SessionActions";
import { SessionChat } from '../components/SessionChat';
import { OfflineHost, UnavailableTab, UnavailableView, unavailableResource } from '../components/shell/RecoveryStates';
import { useWorkingView, workingId } from "../lib/workingView";
import {SecurityBadge} from '../components/SecurityBadge';
import {frozenSecurity} from '../lib/security';
import { SessionDeliveries } from '../components/SessionDeliveries';
import { Feature, useFeatures } from "../components/Feature";
import { CopyBlock } from "../components/CopyBlock";
import clsx from "clsx";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, BookMarked, ChevronDown, Code2,  Download, ExternalLink, GitBranch, GitPullRequest, Pencil, TerminalSquare } from "lucide-react";
import { api, isNotFound, post } from "../lib/api";
import type { AttentionItem, GithubSnapshot, SessionDetail } from "../lib/types";
import { announce, Badge, Button, Card, Dialog, Empty, ErrorNote, Field, IconButton, Input, LoadingRows, Page, Popover, QueryState, Skeleton, useDocumentTitle, useMedia } from "../components/ui";
import { TabPanel, Tabs } from "../components/Tabs";
import { isLive, NowDoing, permissionWords, ProviderBadge, sessionState, StateBadge } from "../components/session-bits";
import { useArtifacts } from '../components/useArtifacts';
import { DocList, DocViewer, type KnowledgeView, KnowledgePanel, useKnowledge } from "../components/Knowledge";
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useInstance } from "../lib/queries";

import { RiskLabel } from "../components/RiskLabel";
import { useDismissableLayer } from "../components/keyboard";
import { lazyImport } from "../lib/lazyImport";



const PromoteButton = lazy(() => lazyImport(() => import('../components/PromoteButton')));
const DeliveryHistory = lazy(() => lazyImport(() => import('../components/DeliveryHistory')).then(m => ({ default: m.DeliveryHistory })));
const SessionAutomation = lazy(() => lazyImport(() => import('../components/SessionAutomation')).then(m => ({ default: m.SessionAutomation })));
const Setup = lazy(() => lazyImport(() => import('../components/SessionSetup')).then(m => ({ default: m.SessionSetup })));
const Artifacts = lazy(() => lazyImport(() => import('../components/Artifacts')).then(m => ({ default: m.Artifacts })));
const Activity = lazy(() => lazyImport(() => import('../components/SessionActivity')).then(m => ({ default: m.SessionActivity })));
const InboxPermissionSetting = lazy(() => lazyImport(() => import('../components/SessionSetup')).then(m => ({ default: m.InboxPermissionSetting })));
const Files = lazy(() => lazyImport(() => import("../components/Files")).then((m) => ({ default: m.Files })));
const Changes = lazy(() => lazyImport(() => import("../components/Changes")).then((m) => ({ default: m.Changes })));
const SessionRelayPanel = lazy(() => import('../components/session-relay/SessionRelayPanel').then(m => ({ default: m.SessionRelayPanel })));
const RegisteredTerminal = lazy(() => lazyImport(() => import("../components/RegisteredTerminal")).then(m => ({ default: m.RegisteredTerminal })));
const Preview = lazy(() => lazyImport(() => import("../components/Preview")).then((m) => ({ default: m.Preview })));
const PullRequestPanel = lazy(() => lazyImport(() => import("../components/PullRequest")).then((m) => ({ default: m.PullRequestPanel })));
const TeamPanel = lazy(() => lazyImport(() => import("../components/TeamPanel")).then((m) => ({ default: m.TeamPanel })));

function usePendingRisk(session: SessionDetail) {
  const view = useWorkingView();
  return useQuery({
    queryKey: ["attention", "pending", session.id],
    queryFn: () => api<AttentionItem[]>(`/api/attention?scope=pending&session_id=${session.id}`),
    refetchInterval: view && !view.visible ? false : 3000, enabled: !view || view.visible,
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

const TABS = ["Terminal", "Chat", "Files", "Preview", "Changes", "Pull request", "Artifacts", "Activity", "Handoffs", "Setup", "Team"] as const;
type Tab = (typeof TABS)[number];
const hasPullTab = (session: SessionDetail) => !!session.repo && !session.standalone;
const tabLabel = (t: Tab, standalone: boolean) => (t === "Handoffs" && standalone ? "Findings" : t);

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

const ciTone = (run: { status: string; conclusion: string | null }) =>
  run.status !== "completed" ? "accent" : run.conclusion === "success" ? "ok"
    : ["failure", "timed_out", "startup_failure"].includes(run.conclusion ?? "") ? "bad" : "neutral";

/** The branch the session's folder is on and, from the project's GitHub view, the open pull request from it and its CI. */
function BranchAndPull({ session }: { session: SessionDetail }) {
  const view = useWorkingView();
  const branch = session.changes?.branch ?? session.worktree_branch;
  const github = useQuery({
    queryKey: ["github", String(session.project_id)], queryFn: () => api<GithubSnapshot>(`/api/projects/${session.project_id}/github`),
    refetchInterval: 60000, enabled: !!branch && !session.standalone && (!view || view.visible),
  });
  if (!branch) return null;
  const pull = github.data?.pulls?.find((p) => p.head === branch && !!p.head_repo && p.head_repo.toLowerCase() === session.repo?.toLowerCase());
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
          <GitPullRequest size={12} aria-hidden="true" /> #{pull.number} <Badge>{(pull.lifecycle ?? (pull.draft ? "draft" : "open")).replaceAll("_", " ")}</Badge>
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
function OpenInEditor({ editor, inline = false }: { editor: NonNullable<SessionDetail["editor"]>; inline?: boolean }) {
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
        <div className={inline ? "mt-1.5 w-full min-w-0 space-y-3 border-t border-line pt-3" : "absolute right-0 z-20 mt-1.5 w-[22rem] max-w-[calc(100vw-2rem)] space-y-3 rounded-[10px] border border-line-strong bg-panel p-3 shadow-lg"}>
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

function SessionTabs({ session, tab, setTab, chatAvailable }: { session: SessionDetail; tab: Tab; setTab: (t: Tab) => void; chatAvailable: boolean }) {
  const working = useWorkingView();
  useLayoutEffect(() => {
    if (working?.visible && working.focusTab === `${session.id}:${tab}`)
      document.getElementById(`${workingId('session-tabs', working)}-tab-${tab.replaceAll(' ', '-')}`)?.focus();
  }, [working?.intent, working?.focusTab, working?.visible, session.id, tab]);
  const feature = useFeatures();

  const small = useMedia("(max-width: 639px)");
  const compact = !small;
  const artifacts = useArtifacts(session).data?.files.length ?? 0;
  const relay = session.session_relay === true;
  const shown = TABS.filter((t) => (!relay || t !== "Chat") && (t !== "Chat" || (feature("chat") && (chatAvailable || tab === "Chat"))) && (t !== "Preview" || feature("live_preview")) && (t !== "Team" || feature("teams")) && (t !== "Team" || session.team_id === session.id) && (t !== "Artifacts" || artifacts > 0 || tab === "Artifacts") && (t !== "Pull request" || hasPullTab(session)));
  return <Tabs id="session-tabs" label="Session views" tabs={shown.map((t) => ({ id: t, label: tabLabel(t, session.standalone),
    count: t === "Changes" ? session.changes?.files || undefined : t === "Artifacts" ? artifacts || undefined : undefined }))} value={tab} onChange={setTab} className={clsx("shrink-0", "session-views-compact")}
    phone={compact ? shown.filter(t => !["Setup", "Activity", "Handoffs"].includes(t)) : ["Terminal", "Chat", "Changes"]} />;
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
  const working = useWorkingView();

  const [terminalTools, setTerminalTools] = useState<HTMLDivElement | null>(null);
  const { id = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const tabParam = search.get("tab");
  const requested: Tab = tabParam === "chat" ? "Chat" : tabParam === "team" ? "Team" : tabParam === "activity" ? "Activity" : ["spec", "automation", "goal"].includes(tabParam ?? "") ? "Setup" : tabParam === "handoffs" ? "Handoffs"
    : tabParam === "files" ? "Files" : tabParam === "preview" ? "Preview" : tabParam === "changes" ? "Changes" : tabParam === "pull" ? "Pull request" : tabParam === "artifacts" ? "Artifacts" : "Terminal";
  const setTab = (t: Tab) =>
    setSearch(t === "Terminal" ? {} : { tab: t === "Chat" ? "chat" : t === "Activity" ? "activity" : t === "Handoffs" ? "handoffs" : t === "Changes" ? "changes" : t === "Pull request" ? "pull"
      : t === "Preview" ? "preview" : t === "Files" ? "files" : t === "Artifacts" ? "artifacts" : t === "Team" ? "team" : "spec" }, { replace: true, state: { focusTab: `${id}:${t}` } });
  const query = useQuery({ queryKey: ["sessions", id], queryFn: () => api<SessionDetail>(`/api/sessions/${id}`), refetchInterval: working && !working.visible ? false : 3000, enabled: !working || working.visible,
    retry: (count, error) => !(unavailableResource(error)) && count < 3 });
  const session = query.data;
  const [chatAvailability, setChatAvailability] = useState({ id: "", available: false });
  const onChatAvailability = useCallback((id: string, available: boolean) => setChatAvailability(previous => previous.id === id && previous.available === available ? previous : { id, available }), []);
  const { host, features: relayFeatures } = useInstance();
  const feature = useFeatures();
  const [editingAutomation, setEditingAutomation] = useState(false);
  useDocumentTitle(unavailableResource(query.error) ? undefined : session?.name);
  useAnnounceState(unavailableResource(query.error) || working && !working.visible ? undefined : session);
  if (unavailableResource(query.error)) return <Page><UnavailableView onRetry={() => { void query.refetch(); }} /></Page>;
  if (!session) return <Page>{isNotFound(query.error) ? (
    <Card><Empty icon={TerminalSquare} action={<Link to="/sessions" className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2 underline underline-offset-2"><ArrowLeft size={14} aria-hidden="true" /> Back to sessions</Link>}>
      <span className="block text-[15px] font-semibold text-fg">There's no session here</span>
      It was archived and removed, the link is mistyped, or it belongs to someone else.
    </Empty></Card>
  ) : query.error ? <ErrorNote error={query.error} title="Couldn't load this session." onRetry={query.refetch} /> : <Skeleton className="h-64" />}</Page>;
  const tab = availableTab(session, requested);
  const knownTabs = ['chat', 'team', 'activity', 'spec', 'automation', 'goal', 'handoffs', 'files', 'preview', 'changes', 'pull', 'artifacts', 'terminal'];
  if (requested === 'Chat' && !feature('chat')) return <Page><Feature name="chat" page>{null}</Feature></Page>;
  if ((tabParam && !knownTabs.includes(tabParam)) || tab !== requested || (requested === 'Preview' && !feature('live_preview')) || (requested === 'Team' && !feature('teams'))) return <Page><UnavailableTab sessionId={session.id} onRetry={() => { void query.refetch(); }} /></Page>;

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
    <div className={clsx("flex h-full flex-col gap-3 px-4 pb-4 pt-3 md:gap-4 md:px-8 md:pb-6 md:pt-5", "shell-session-page")}>
      {<div className="session-heading shell-session-heading">
        <div className="session-primary-line">
          <h1 title={session.name}>{session.name}</h1>
          <ProviderBadge provider={session.provider} model={session.model} /><StateBadge session={session} />
          <SecurityBadge policy={frozenSecurity(session.launch_spec.files)} frozen older={session.security_older_settings} label="This session" />
          {waiting && tab === "Terminal" && <HeaderRisk session={session} />}
          <div className="session-delivery-slot"><SessionDeliveries key={session.id} id={session.id} onDetails={() => setSearch({ tab: "activity", delivery: "details" }, { replace: true })} /></div>
          <Popover label="Session details" className="session-details-popover" wide trigger={<>Details <ChevronDown size={13} aria-hidden="true" /></>}>
            {close => <div className="space-y-3 p-1.5">
              <div className="flex min-w-0 flex-wrap items-center gap-3 text-xs text-muted">{projectLink}{meta(close)}</div>
              <p className="break-all font-mono text-xs text-muted">{session.worktree ?? session.cwd}</p>
              <NowDoing session={session} className="text-xs text-muted" />
              {session.native_helpers_capped === false && <p className="text-xs text-muted">Provider-native helpers are not capped or tracked by Rynhult. <Link className="underline" to="/guide/User-Guide/Orchestrator#parent-permission-limits">Managed helper permissions</Link></p>}
              <div className="flex flex-wrap items-center gap-2"><RenameButton session={session} />{exportLink}{session.editor && <OpenInEditor editor={session.editor} inline />}{session.standalone && <PromoteButton session={session} />}<SessionActions session={session} secondaryOnly /></div>
            </div>}
          </Popover>
          <SessionActions session={session} compact primaryOnly />
        </div>
        <div className="session-tab-line"><SessionTabs session={session} tab={tab} setTab={setTab} chatAvailable={chatAvailability.id === session.id && chatAvailability.available} />{tab === "Terminal" && <div className="session-terminal-tools" ref={setTerminalTools} />}</div>
      </div>}
      <TabPanel id="session-tabs" value={tab} className={clsx("min-h-[420px] flex-1", tab === "Terminal" && "shell-terminal-panel")}>
        {session.machine_offline && (<OfflineHost host={session.host} />)}
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
        {session.session_relay === true ? (<div hidden={tab !== "Terminal" && tab !== "Chat"}><Suspense><SessionRelayPanel sessionId={session.id} projectId={session.project_id} enabled={relayFeatures?.session_relay === true} /></Suspense></div>) : feature("chat") ? <SessionChat key={session.id} session={session} tab={tab} onTerminal={() => { setTab("Terminal"); requestAnimationFrame(() => document.getElementById(`${workingId("session-tabs", working)}-tab-Terminal`)?.focus()); }} onChat={() => setTab("Chat")} onAvailability={onChatAvailability}
          terminal={session.management !== "external" && (session.machine_offline
          ? null : isLive(session) || session.observed_state === "exited"
          ? <RegisteredTerminal sessionId={session.id} toolbarTarget={terminalTools} />
          : <Card><Empty icon={TerminalSquare}>This session isn't running. Resume it to open its terminal.</Empty></Card>)} />
          : tab === "Terminal" && session.management !== "external" && (session.machine_offline
            ? null
            : isLive(session) || session.observed_state === "exited"
              ? <RegisteredTerminal sessionId={session.id} toolbarTarget={terminalTools} />
              : <Card><Empty icon={TerminalSquare}>This session isn't running. Resume it to open its terminal.</Empty></Card>)}
        {tab === "Team" && session.project_id !== null && !session.machine_offline && session.team_id === session.id && <Feature name="teams"><TeamPanel id={session.id} projectId={session.project_id} /></Feature>}
        {feature("live_preview") && tab === "Preview" && session.project_id !== null && !session.machine_offline && <div className="grid min-w-0 gap-4 xl:grid-cols-2">
          <Preview key={session.id} projectId={session.project_id} projectName={session.project}
            folder={session.worktree ?? session.cwd} branch={session.changes?.branch ?? session.worktree_branch} sessionId={session.id} />
          <div className="min-w-0">{session.management !== "external" && (isLive(session) || session.observed_state === "exited")
            ? <Link to={`/sessions/${session.id}`}>Open terminal view</Link> : <Activity id={session.id} />}</div>
        </div>}
        {tab === "Activity" && <div className="space-y-4"><DeliveryHistory id={session.id} focus={search.get("delivery") === "details"} /><Activity id={session.id} /></div>}

        {tab === "Files" && !session.machine_offline && <Files kind="session" scopeKey={session.id} />}
        {tab === "Changes" && !session.machine_offline && <Changes session={session} />}
        {tab === "Pull request" && hasPullTab(session) && <PullRequestPanel session={session} />}
        {tab === "Artifacts" && !session.machine_offline && <Artifacts session={session} />}
        {tab === "Setup" && <div className="space-y-4">
          {session.provider === "claude" && session.management === "acc" && !session.container && <InboxPermissionSetting id={session.id} />}
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
