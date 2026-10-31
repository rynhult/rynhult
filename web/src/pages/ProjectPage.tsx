import { FirstCommit } from "../components/FirstCommit";
import { ProjectDeleteDialog } from "../components/ProjectDelete";
import { GitIdentityCard } from "../components/GitIdentity";
import { ProjectBrief } from '../components/ProjectBrief';
import { Feature, useFeatures } from "../components/Feature";
import { desktopBuild } from "../lib/desktop";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { Files } from "../components/Files";
import clsx from "clsx";
import { useLayoutEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, BookOpen, CheckCircle2, CircleDot, ExternalLink, FolderGit2, GitBranch, GitCommitHorizontal, GitPullRequest, Container, Loader2, PlayCircle, RefreshCw, Rocket, TerminalSquare, XCircle, Plus } from "lucide-react";
import { api } from "../lib/api";
import { ago, isLocalProject } from "../lib/format";
import type { BackupView, ComposeReview, Container as RuntimeContainer, GithubSnapshot, Project } from "../lib/types";
import { useInstance, useRole, useSessions } from "../lib/queries";
import { SessionList } from "../components/SessionList";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, IconButton, LoadingRows, Page, PageHeader, Skeleton, TabPanel, Tabs } from "../components/ui";
import { useNewSession } from "../components/newSession";
import { isLive } from "../components/session-bits";
import { KnowledgePanel } from "../components/Knowledge";
import { Preview } from "../components/Preview";
import { PortsCard } from "../components/Ports";
import { ProjectKeysCard } from "../components/Keys";
import { ProjectIssueSources } from "../components/IssueSources";
import { BackupCard } from "../components/Backup";
import { ProjectContainersCard, type ContainersView } from "../components/Containers";
import { ProjectGatesCard } from "../components/Gates";
import { Runtime } from "./Projects";

type Snapshot = GithubSnapshot;

function RunIcon({ status, conclusion }: { status: string; conclusion: string | null }) {
  if (status !== "completed") return <Loader2 size={14} className="animate-spin text-accent" aria-label="Running" />;
  if (conclusion === "success") return <CheckCircle2 size={14} className="text-ok" aria-label="Passed" />;
  if (conclusion === "failure" || conclusion === "timed_out") return <XCircle size={14} className="text-bad" aria-label="Failed" />;
  return <CircleDot size={14} className="text-muted" aria-label={conclusion ?? status} />;
}

const Row = ({ href, children }: { href: string; children: ReactNode }) => (
  <a href={href} target="_blank" rel="noreferrer" className="flex items-start gap-2.5 px-4 py-2.5 text-[13px] hover:bg-panel-2/70">{children}</a>
);

/** A row with a button beside the link (a button can't sit inside an <a>). */
const ActionRow = ({ href, action, children }: { href: string; action?: ReactNode; children: ReactNode }) => (
  <div className="flex items-center hover:bg-panel-2/70">
    <a href={href} target="_blank" rel="noreferrer" className="flex min-w-0 flex-1 items-start gap-2.5 px-4 py-2.5 text-[13px]">{children}</a>
    {action && <div className="shrink-0 pr-3">{action}</div>}
  </div>
);

const issuePrompt = (repo: string, i: { number: number; title: string; url: string }) =>
  `Work on GitHub issue #${i.number} in ${repo}: ${i.title}. ${i.url}. Read it with gh issue view ${i.number} first, `
  + "then make the change on this branch and open a pull request that closes it.";
const reviewPrompt = (repo: string, pr: { number: number; url: string }) =>
  `Review pull request #${pr.number} in ${repo} (${pr.url}): check it out with gh pr checkout ${pr.number}, read the diff, `
  + "run the tests, and post your review with gh pr review. Don't push changes.";

/** Subscribe to the cards' existing cache entries without changing their fetch/poll options. */
function useCachedProjectQuery<T = unknown>(key: readonly unknown[]) {
  const client = useQueryClient();
  const snapshot = () => client.getQueryState<T>(key);
  const state = useSyncExternalStore(notify => client.getQueryCache().subscribe(notify), snapshot, snapshot);
  return { data: state?.data, error: state?.error, isError: state?.status === "error",
    refetch: () => client.refetchQueries({ queryKey: key, exact: true }) };
}

/** Same query keys as the mounted cards: status stays visible when controls are closed. */
function ProjectStatus({ project: p, owner }: { project: Project; owner: boolean }) {
  const backup = useCachedProjectQuery<BackupView>(["backup", p.id]);
  const runtimeEnabled = !desktopBuild && owner && p.import_mode === "managed" && !p.machine_offline;
  const runtime = useCachedProjectQuery<{ containers: RuntimeContainer[]; note?: string; not_started?: boolean; compose?: ComposeReview }>(["runtime", p.id]);
  const keys = useCachedProjectQuery(["project-keys", p.id]);
  const ports = useCachedProjectQuery(["ports"]);
  const containers = useCachedProjectQuery<ContainersView>(["containers"]);
  const b = backup.data;
  const detailLink = (section: string, label: string) => <Link to={{ hash: `project-${section}` }} className="text-accent underline underline-offset-2">{label}</Link>;
  return <section aria-label="Project status" className="min-w-0 space-y-2 break-words text-[13px] text-muted [overflow-wrap:anywhere]">
    {!desktopBuild && <>
      <ErrorNote error={backup.error} title="Couldn't read the backup status." onRetry={backup.refetch} />
      {!b ? !backup.isError && <p>Checking backup…</p> : <>
        <p role={b.error ? "alert" : undefined} className={b.error ? "text-bad" : !b.configured || !b.enabled || !b.last_at || b.git === false ? "text-wait" : undefined}>
          {b.error ? `Last backup attempt failed: ${b.error}` : !b.configured ? "Backups need this Rynhult's https address (ACC_BACKUP_URL in /etc/default/accd)."
            : !b.enabled ? (b.repo ? "Backup off. Previous copies are kept." : "Backup off. No copy recorded.") : b.git === false ? "The project's folder isn't a git repository yet."
            : !b.repo?.branches.length ? "No branches recorded in the backup." : b.last_at ? `Last backup ${ago(b.last_at)}` : b.note ?? "No successful backup recorded yet."}
          {b.checked_at ? ` · checked ${ago(new Date(b.checked_at * 1000).toISOString())}` : " · not checked yet"} · {detailLink("backup", "Backup")}
        </p>
        {!!b.kept?.length && <p className="text-wait">Rewritten upstream references were kept under new names. {detailLink("backup", "Review backup")}</p>}
      </>}
    </>}
    <ErrorNote error={keys.error} title="Couldn't read the project keys." onRetry={keys.refetch} />
    {!desktopBuild && p.import_mode === "managed" && <ErrorNote error={ports.error} title="Couldn't read the reserved ports." onRetry={ports.refetch} />}
    {runtimeEnabled && <>
      <ErrorNote error={runtime.error} title="Couldn't read the runtime status." onRetry={runtime.refetch} />
      {!runtime.data && !runtime.isError && <p>Checking runtime…</p>}
      {runtime.data && !runtime.data.containers.length && !runtime.data.note && !runtime.data.not_started &&
        <p className="text-wait">Stack not running. {detailLink("stack", "Runtime")}</p>}
      {p.compose_autostart && runtime.data && !runtime.data.compose &&
        <p className="text-wait">Autostart approval status unavailable. {detailLink("stack", "Runtime")}</p>}
      {runtime.data?.note && !(runtime.data.note.includes("no compose file") && !runtime.data.containers.length) && <p>{runtime.data.note} · {detailLink("stack", "Runtime")}</p>}
      {runtime.data?.containers.filter(c => c.State !== "running" || c.Health === "unhealthy").map(c =>
        <p key={c.Name} role={c.State === "exited" && c.Status?.startsWith("Exited (0)") ? undefined : "alert"} className={c.State === "exited" && c.Status?.startsWith("Exited (0)") ? undefined : "text-wait"}>{c.Service}: {c.Status || c.State}{c.Health ? ` · ${c.Health}` : ""} · {detailLink("stack", "Runtime")}</p>)}
      {p.compose_autostart && runtime.data?.compose && !runtime.data.compose.approved &&
        <p role="alert" className="text-wait">Autostart waits until an owner approves the current compose files. {detailLink("stack", "Review runtime")}</p>}
    </>}
    {p.containers && !desktopBuild && owner && p.import_mode === "managed" && !p.private_workspace && <>
      <ErrorNote error={containers.error} title="Couldn't read the container policy." onRetry={containers.refetch} />
      {containers.data && !containers.data.enabled && <p role="alert" className="text-bad">Containers are off on this Rynhult, so this project's sessions can't start. {detailLink("stack", "Runtime")}</p>}
      {!containers.data && !containers.isError && <p>Checking container policy…</p>}
    </>}
  </section>;
}

/** The Overview's sections, as tabs nested under Overview/Files/Preview (UX follow-up to W7). Each is deep-linkable as
 *  #project-<id>; #project-settings, the old Project settings heading, opens Keys. */
type Section = "brief" | "sessions" | "repository" | "knowledge" | "keys" | "backup" | "ports" | "stack" | "gates";
function projectSections(p: Project, owner: boolean, orchestrator: boolean): { id: Section; label: string }[] {
  return [{ id: "sessions", label: "Sessions" }, ...(!p.private_workspace ? [{ id: "repository" as const, label: "Issues and pull requests" }] : []),
    ...(!desktopBuild && orchestrator && p.import_mode === "managed" && !p.private_workspace ? [{ id: "gates" as const, label: "Gate queue" }] : []),
    ...(orchestrator ? [{ id: "brief" as const, label: "Project brief" }] : []), { id: "knowledge", label: "Knowledge" }, { id: "keys", label: "Keys" }, ...(!desktopBuild ? [{ id: "backup" as const, label: "Backup" }] : []),
    ...(!desktopBuild && p.import_mode === "managed" ? [{ id: "ports" as const, label: "Ports" }, ...(owner ? [{ id: "stack" as const, label: "Runtime" }] : [])] : [])];
}
const sectionOf = (hash: string, sections: { id: Section }[]): Section => {
  const wanted = hash === "#project-settings" ? "keys" : hash.replace(/^#project-/, "");
  return sections.find((x) => x.id === wanted)?.id ?? "sessions";
};

export function ProjectPage() {
  const [deleting, setDeleting] = useState(false);
  const feature = useFeatures();
  const { id = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const { hash, key } = useLocation();
  const tab = hash.startsWith("#project-") ? "overview" : search.get("tab") === "files" ? "files" : search.get("tab") === "preview" ? "preview" : "overview";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const project = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), select: (all) => all.find((p) => String(p.id) === id) });
  const sessions = useSessions();
  const { owner } = useRole();
  const { orchestrator } = useInstance();
  const newSession = useNewSession();
  const isGithub = !!project.data?.repo && (!project.data.remote_url || /^(git@|https:\/\/|ssh:\/\/git@)github\.com[:/]/.test(project.data.remote_url));
  const github = useQuery({ queryKey: ["github", id], queryFn: () => api<Snapshot>(`/api/projects/${id}/github`), refetchInterval: 60000, enabled: isGithub });
  const refresh = useMutation({
    mutationFn: () => api<Snapshot>(`/api/projects/${id}/github?refresh=1`),
    onSuccess: (data) => queryClient.setQueryData(["github", id], data),
  });
  const p = project.data;
  const sections = p ? projectSections(p, owner, !!orchestrator) : [];
  const section = sectionOf(hash, sections);
  useLayoutEffect(() => {
    // A link to a section (status line, bookmark, Back): show its tab and put focus on it, so Enter/arrows work next.
    if (!hash.startsWith("#project-") || !p) return;
    const tabButton = document.getElementById(`project-sections-tab-${section}`);
    tabButton?.scrollIntoView({ block: "nearest" });
    // Focus with this committed selection. A deferred frame can steal focus from a later
    // arrow-key selection before its navigation commits, making the following arrow start at the wrong tab.
    tabButton?.focus({ preventScroll: true });
  }, [hash, key, p?.id, owner, section]);
  if (!p) return <Page>{project.isLoading ? <Skeleton className="h-40" /> : project.error
    ? <ErrorNote error={project.error} title="Couldn't load the projects." onRetry={project.refetch} />
    : <Card><Empty icon={FolderGit2} action={<Link to="/projects" className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2"><ArrowLeft size={14} aria-hidden="true" /> Back to projects</Link>}>
      <span className="block text-[15px] font-semibold text-fg">There's no project here</span>
      It was removed, the link is mistyped, or it belongs to someone else.
    </Empty></Card>}</Page>;
  const g = isGithub ? github.data : undefined;
  const mine = (sessions.data ?? []).filter((s) => s.project_id === p.id && s.desired_state !== "archived");
  const live = mine.filter(isLive).length;
  const mainRun = g?.runs?.find((r) => r.branch === g.repo?.default_branch && r.status === "completed");
  // Same rules as New session: only managed, trusted projects can launch.
  const canLaunch = p.import_mode === "managed" && p.trusted && !p.archived;
  const start = (name: string, initial_prompt: string) => newSession({ mode: "project", project_id: p.id, name, worktree: true, initial_prompt });
  return (
    <Page wide>
      {p.import_mode === "managed" && !p.archived && <FirstCommit key={p.id} projectId={p.id} />}
      <PageHeader title={p.name} description={p.notes}
        meta={<>
          <span className="font-mono">{p.workspace_path}</span>{p.machine_offline && <Badge>Machine offline</Badge>}{p.private_workspace && <Badge>Development · local only</Badge>}
          {p.import_mode === "readonly" ? <Badge tone="wait">read-only</Badge> : !p.trusted && <Badge tone="wait">not trusted</Badge>}
        </>}
        actions={<>
          {!p.private_workspace && !isLocalProject(p) && <Button variant="danger" onClick={() => setDeleting(true)}>Delete project</Button>}
          {/* Work starts here (UX review PJ-1); an untrusted project gets "Trust and launch" inside New session. */}
          {p.import_mode === "managed" && !p.archived && <Button variant="primary" onClick={() => newSession({ mode: "project", project_id: p.id, ...(p.private_workspace && { worktree: true }) })}>
            <Plus size={14} aria-hidden="true" /> New session</Button>}
          <Link to={`/projects/${p.id}/wiki`} className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong bg-panel-2 px-3 text-[13px] font-medium hover:bg-panel-3">
            <BookOpen size={14} aria-hidden="true" /> Wiki
          </Link>
          {(p.repo || p.remote_url?.startsWith("https://")) && <>
          <a href={p.remote_url?.startsWith("https://") ? p.remote_url : `https://github.com/${p.repo}`} target="_blank" rel="noreferrer"
            className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong bg-panel-2 px-3 text-[13px] font-medium hover:bg-panel-3">
            <ExternalLink size={14} aria-hidden="true" /> {p.repo ?? p.remote_url}
          </a>
          {isGithub && <IconButton icon={RefreshCw} label="Refresh from GitHub" onClick={() => refresh.mutate()} disabled={refresh.isPending}
            className={clsx("border border-line-strong bg-panel-2", refresh.isPending && "[&_svg]:animate-spin")} />}
          </>}
        </>} />

      {deleting && <ProjectDeleteDialog project={p} onClose={() => setDeleting(false)} />}
      <Tabs id="project-tabs" label="Project views" tabs={[{ id: "overview", label: "Overview" }, { id: "files", label: "Files" }, ...(feature("live_preview") ? [{ id: "preview", label: "Preview" }] : [])]} value={tab}
        onChange={(value) => setSearch(value === "overview" ? {} : { tab: value }, { replace: true })} />
      <TabPanel id="project-tabs" value={tab} className="space-y-6">
      {p.machine_offline && tab !== "overview" ? <Card><Empty>Machine offline. This view returns when the machine checks in.</Empty></Card> : tab === "files" ? <Files kind="project" scopeKey={id} /> : tab === "preview" && feature("live_preview") ? <Preview projectId={p.id} projectName={p.name} folder={p.workspace_path} /> : <>
      <section aria-label="Project summary" className="min-w-0 flex flex-wrap gap-x-5 gap-y-1 break-words text-[13px] text-muted [overflow-wrap:anywhere]">
        <p>{sessions.data ? `${live} live session${live === 1 ? "" : "s"} · ${mine.length} in this project` : sessions.isError ? "Sessions unavailable" : "Checking sessions…"}</p>
        {!p.private_workspace && <>
          <p className={mainRun && mainRun.conclusion !== "success" ? "text-bad" : undefined}>
            {!isGithub ? "CI not available for this repository" : github.isError || g?.error ? "CI unavailable" : !g ? "Checking CI…" : !mainRun ? "No completed CI runs" : mainRun.conclusion === "success" ? "CI passing" : "CI failing"}
            {mainRun && <> · {mainRun.name} · {ago(mainRun.created_at)}</>}
          </p>
          <p>{g?.pulls ? `${g.pulls.length} open pull request${g.pulls.length === 1 ? "" : "s"}` : "Pull requests not loaded"}</p>
          <p>{g?.issues ? `${g.issues.length} open issue${g.issues.length === 1 ? "" : "s"}` : "Issues not loaded"}</p>
        </>}
      </section>
      <ProjectStatus key={p.id} project={p} owner={owner} />
      {isGithub && <ErrorNote error={github.error} title="Couldn't read GitHub status." onRetry={github.refetch} />}

      <Tabs id="project-sections" label="Project sections" tabs={sections} value={section}
        onChange={(value) => { navigate({ hash: `project-${value}` }, { replace: true }); }} />
      {/* Every section stays mounted (only the chosen one shows): drafts survive switching, and the status line above
          keeps reading the cards' own queries. */}
      <TabPanel key={p.id} id="project-sections" value={section} className="min-w-0 space-y-6">
        <div id="project-brief" hidden={section !== "brief"}>{orchestrator && <ProjectBrief key={p.id} projectId={p.id} />}</div>
        <div id="project-sessions" hidden={section !== "sessions"} className="min-w-0 space-y-6">
          <Card>
            <CardHeader title="Sessions" icon={TerminalSquare} subtitle="Running and stopped sessions in this project" />
            {sessions.error && <ErrorNote error={sessions.error} title="Couldn't read the sessions." onRetry={sessions.refetch} />}
            {!sessions.data ? <LoadingRows rows={2} /> : mine.length ? <SessionList sessions={mine} showProject={false} />
              : <Empty icon={TerminalSquare}>No sessions yet. Start one with New session.</Empty>}
          </Card>
          {p.repo && g?.repo && (
            <Card>
              <CardHeader title="Actions" icon={PlayCircle} subtitle={`Latest workflow runs · checked ${ago(new Date((g.fetched_at ?? 0) * 1000).toISOString())}`} />
              {g.runs?.length ? <div className="divide-y divide-line">{g.runs.slice(0, 10).map((r) => (
                <Row key={r.id} href={r.url}>
                  <RunIcon status={r.status} conclusion={r.conclusion} />
                  <span className="min-w-0 flex-1 truncate"><span className="font-medium text-fg">{r.name}</span> <span className="text-muted">on {r.branch} · {r.event}</span></span>
                  <span className="tnum shrink-0 text-xs text-faint">{ago(r.created_at)}</span>
                </Row>))}</div> : <Empty>No workflow runs.</Empty>}
            </Card>
          )}

        </div>
        <div id="project-repository" hidden={section !== "repository"} className="min-w-0 space-y-6">
          {p.import_mode === "managed" && <GitIdentityCard key={p.id} projectId={p.id} />}
          {!p.private_workspace && <Feature name="issue_sources"><ProjectIssueSources project={p} canLaunch={canLaunch} /></Feature>}
          {p.private_workspace ? null : !isGithub ? (!p.remote_url && <Card><Empty icon={GitBranch}>No GitHub repository linked to this project.</Empty></Card>) : g?.error ? (
            <Card><p role="alert" className="p-4 text-[13px] text-bad">GitHub: {g.error}</p></Card>
          ) : !g?.repo ? <Card><LoadingRows rows={4} /></Card> : (
            <>
              <Card>
                <CardHeader title="Pull requests" icon={GitPullRequest} subtitle="Open" />
                {g.pulls?.length ? <div className="divide-y divide-line">{g.pulls.map((pr) => (
                  <ActionRow key={pr.number} href={pr.url} action={canLaunch && (
                    <Button size="sm" variant="ghost" aria-label={`Review pull request #${pr.number}`}
                      onClick={() => start(`review-${pr.number}`, reviewPrompt(g.repo!.full_name, pr))}>
                      <Rocket size={13} aria-hidden="true" /> Review
                    </Button>)}>
                    <GitPullRequest size={14} className={clsx("mt-0.5", pr.draft ? "text-muted" : "text-ok")} aria-label={pr.draft ? "Draft" : "Open"} />
                    <span className="min-w-0 flex-1"><span className="font-medium text-fg">#{pr.number} {pr.title}</span><span className="block text-xs text-muted">{pr.head} · {pr.author}</span></span>
                    <span className="tnum shrink-0 text-xs text-faint">{ago(pr.updated_at)}</span>
                  </ActionRow>))}</div> : <Empty>No open pull requests.</Empty>}
              </Card>
              <Card>
                <CardHeader title="Issues" icon={CircleDot} subtitle="Open" />
                {g.issues?.length ? <div className="divide-y divide-line">{g.issues.map((i) => (
                  <ActionRow key={i.number} href={i.url} action={canLaunch && (
                    <Button size="sm" variant="ghost" aria-label={`Start session for issue #${i.number}`}
                      onClick={() => start(`issue-${i.number}`, issuePrompt(g.repo!.full_name, i))}>
                      <Rocket size={13} aria-hidden="true" /> Start session
                    </Button>)}>
                    <CircleDot size={14} className="mt-0.5 text-ok" aria-hidden="true" />
                    <span className="min-w-0 flex-1"><span className="font-medium text-fg">#{i.number} {i.title}</span>
                      {i.labels.length > 0 && <span className="ml-2">{i.labels.map((l) => <Badge key={l} className="mr-1">{l}</Badge>)}</span>}</span>
                    <span className="tnum shrink-0 text-xs text-faint">{ago(i.updated_at)}</span>
                  </ActionRow>))}</div> : <Empty>No open issues.</Empty>}
              </Card>
              <Card>
                <CardHeader title="Recent commits" icon={GitCommitHorizontal} subtitle={`On ${g.repo.default_branch}`} />
                <div className="divide-y divide-line">{g.commits?.map((c) => (
                  <Row key={c.sha} href={c.url}>
                    <span className="mt-0.5 font-mono text-xs text-faint">{c.sha}</span>
                    <span className="min-w-0 flex-1 truncate text-fg">{c.message}</span>
                    <span className="shrink-0 text-xs text-faint">{c.author} · {ago(c.date)}</span>
                  </Row>))}</div>
                <div className="flex flex-wrap gap-1.5 border-t border-line px-4 py-3">
                  {g.branches?.map((b) => <Badge key={b.name} tone={b.name === g.repo!.default_branch ? "accent" : "neutral"}><GitBranch size={11} aria-hidden="true" /> {b.name}{b.protected ? " · protected" : ""}</Badge>)}
                </div>
              </Card>
            </>
          )}
        </div>
        {!desktopBuild && orchestrator && p.import_mode === "managed" && !p.private_workspace && <div id="project-gates" hidden={section !== "gates"} className="min-w-0"><ProjectGatesCard project={p} /></div>}
        <div id="project-knowledge" hidden={section !== "knowledge"} className="min-w-0"><KnowledgePanel projectId={p.id} /></div>
        <div id="project-keys" hidden={section !== "keys"} className="min-w-0"><ProjectKeysCard projectId={p.id} /></div>
        {!desktopBuild && <div id="project-backup" hidden={section !== "backup"} className="min-w-0"><BackupCard project={p} /></div>}
        {!desktopBuild && p.import_mode === "managed" && <div id="project-ports" hidden={section !== "ports"} className="min-w-0"><PortsCard projectId={p.id} /></div>}
        {!desktopBuild && p.import_mode === "managed" && owner && <div id="project-stack" hidden={section !== "stack"} className="min-w-0 space-y-6">
          {!p.private_workspace && <ProjectContainersCard project={p} />}
          <Card>
            <CardHeader title="Docker stack" icon={Container} subtitle="Its compose containers, and what Autostart may run" />
            <div className="p-4">{p.machine_offline ? <p className="text-xs text-muted">Machine offline</p> : <Runtime project={p} />}</div>
          </Card>
        </div>}
      </TabPanel>
      </>}
      </TabPanel>
    </Page>
  );
}
