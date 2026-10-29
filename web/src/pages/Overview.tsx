import { MachineState } from "../components/MachinePolicy";
import { useNodes } from "../components/Machines";
import clsx from "clsx";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRight, BellRing, Check, ChevronRight, Container, Cpu, FolderGit2, FolderPlus, Gauge, HardDrive, Plus, Rocket, Smartphone, TerminalSquare, X } from "lucide-react";
import { api } from "../lib/api";
import { ago, isLocalProject, until } from "../lib/format";
import { useAttention, useInstance, useProjects, useRole, useSessions } from "../lib/queries";
import type { AccountHealth, HostView, Observation, Project } from "../lib/types";
import { Button, Card, CardHeader, Dot, Empty, ErrorNote, IconButton, LoadingRows, Meter, Page, PageHeader, QueryState, Stat } from "../components/ui";
import { NewProjectDialog } from "../components/NewProject";
import { useNewSession } from "../components/newSession";
import { groupLive, isLive } from "../components/session-bits";
import { useBudgets } from "../components/Budgets";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { SessionList } from "../components/SessionList";
import { needsYou, sessionCount, SESSION_GROUPS, sortInbox } from "../lib/order";
import { installedApp } from "../lib/app";

export const useHost = () => useQuery({ queryKey: ["host"], queryFn: () => api<HostView>("/api/host"), refetchInterval: 15000 });
// Plan usage is the main account's: owners only (members' pages don't ask; they'd be refused).
const useObservations = () => {
  const { owner } = useRole();
  return useQuery({ queryKey: ["observations"], queryFn: () => api<Observation[]>("/api/observations"), refetchInterval: 30000, enabled: owner });
};

function UsageRow({ label, pct, resets, budget }: { label: string; pct: number; resets: number; budget?: { allowed: number; over: boolean } }) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between text-xs">
        <span className="text-muted">{label}</span>
        <span className="tnum text-fg">{pct}%<span className="text-faint"> · resets {until(resets)}</span>
          {budget && <Link to="/settings#budgets" className={clsx("inline-flex min-h-6 pointer-coarse:min-h-11 items-center underline underline-offset-2", budget.over ? "text-wait" : "text-faint")} title="Your budget (Settings → Budgets)"> · Budget details ({Math.round(budget.allowed)}%)</Link>}</span>
      </div>
      <Meter value={pct} label={`${label} used`} />
    </div>
  );
}

function PlanUsage() {
  const observations = useObservations();
  const claude = observations.data?.find((o) => o.key === "claude.rate_limits");
  const codex = observations.data?.find((o) => o.key === "codex.rate_limits");
  const copilot = observations.data?.find((o) => o.key === "copilot.usage");
  const credits = copilot?.value?.credits;
  const seen = (o?: Observation) => (o ? ago(new Date(o.observed_at * 1000).toISOString()) : "no reading yet");
  const budget = useBudgets().data?.status ?? {};
  return (
    <Card>
      <CardHeader title="Plan usage" icon={Gauge} subtitle="Reported by each CLI; never combined"
        action={<Link to="/providers" className="inline-flex min-h-6 pointer-coarse:min-h-11 items-center text-xs text-muted hover:text-fg underline underline-offset-2">Provider details</Link>} />
      <div className="space-y-4 p-4">
        <div className="space-y-2.5">
          <div className="flex justify-between text-xs"><span className="font-medium text-fg">Claude Max</span><span className="text-faint">{seen(claude)}</span></div>
          {claude ? ([["five_hour", "5-hour window"], ["seven_day", "7-day window"]] as const).map(([key, label]) => {
            const w = claude.value[key];
            return w ? <UsageRow key={key} label={label} pct={Math.round(w.used_percentage ?? 0)} resets={w.resets_at} budget={budget[`claude.${key}`]} /> : null;
          }) : <p className="text-xs text-faint">Appears after a Claude session's first reply.</p>}
        </div>
        <div className="space-y-2.5 border-t border-line pt-3.5">
          <div className="flex justify-between text-xs"><span className="font-medium text-fg">Codex</span><span className="text-faint">{seen(codex)}</span></div>
          {codex?.value?.primary ? (
            <UsageRow label={codex.value.primary.window_minutes >= 1440 ? "Weekly window" : "5-hour window"}
              pct={Math.round(codex.value.primary.used_percent)} resets={codex.value.primary.resets_at} budget={budget["codex.primary"]} />
          ) : <p className="text-xs text-faint">No reading yet.</p>}
        </div>
        {credits && (
          <div className="space-y-2.5 border-t border-line pt-3.5">
            <div className="flex justify-between text-xs"><span className="font-medium text-fg">Copilot {copilot.value.plan ? copilot.value.plan[0].toUpperCase() + copilot.value.plan.slice(1) : ""}</span><span className="text-faint">{seen(copilot)}</span></div>
            {credits.unlimited ? <p className="text-xs text-muted">Unlimited AI credits</p> : (
              <UsageRow label={`AI credits · ${Number(credits.used ?? 0).toLocaleString()} of ${Number(credits.entitlement ?? 0).toLocaleString()}`}
                pct={Math.round(100 - (credits.percent_remaining ?? 100))} resets={copilot.value.resets_at ? Date.parse(copilot.value.resets_at) / 1000 : 0} budget={budget["copilot.credits"]} />
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

const stored = (key: string) => { try { return localStorage.getItem(key); } catch { return null; } };
const store = (key: string, value: string) => { try { localStorage.setItem(key, value); } catch { /* private window: this visit only */ } };
const usableProject = (p: Project) => !p.archived && p.import_mode === "managed" && !isLocalProject(p);

/** Any agent CLI signed in for your agent account (GitHub doesn't run sessions). */
export const agentSignedIn = (health?: AccountHealth) => Object.entries(health ?? {}).some(([id, v]) =>
  id !== "github" && !!v && typeof v === "object" && !Array.isArray(v) && (v as { logged_in?: boolean }).logged_in === true);

/** The first hour, as steps that tick themselves (UX review 2, FR-1): an agent signed in, a project and a session.
 *  Hidden once done or dismissed. Connecting a phone is the PhoneBanner below, not a step (owner, 2026-10-23). */
function GetStarted() {
  const [hidden, setHidden] = useState(() => stored("acc.getStarted") === "hidden");
  const [creating, setCreating] = useState(false);
  const newSession = useNewSession();
  const projects = useProjects();
  const sessions = useSessions();
  const accounts = useQuery({ queryKey: ["accounts", "mine"], queryFn: () => api<AccountHealth>("/api/accounts"), refetchInterval: 60000, enabled: !hidden });
  if (hidden || !projects.data || !sessions.data || accounts.isPending) return null;
  const project = projects.data.find(usableProject);
  const steps = [
    { id: "agent", done: agentSignedIn(accounts.data), title: "Sign in an agent",
      detail: "Claude Code, Codex or another agent CLI, signed in for your agent account.",
      action: <Link to="/providers" className="inline-flex h-7 pointer-coarse:h-11 items-center rounded-md border border-line-strong px-2.5 text-[12.5px] text-fg hover:bg-panel-2 underline underline-offset-2">Open Providers</Link> },
    { id: "project", done: !!project, title: "Add a project",
      detail: "An empty folder or a clone of a repository. Rynhult makes the folder; no server shell needed.",
      action: <Button size="sm" onClick={() => setCreating(true)}><FolderPlus size={13} aria-hidden="true" /> New project</Button> },
    { id: "session", done: sessions.data.length > 0, title: "Start your first session",
      detail: "Say what to work on, pick an agent and launch. You watch and answer it here.",
      action: <Button size="sm" onClick={() => newSession(project ? { project_id: project.id } : undefined)}><Rocket size={13} aria-hidden="true" /> Start a session</Button> },
  ];
  const left = steps.filter((x) => !x.done).length;
  if (!left) return null;
  return (
    <Card>
      <CardHeader title="Get started" icon={Rocket} subtitle={`${steps.length - left} of ${steps.length} done`}
        action={<Button size="sm" variant="ghost" onClick={() => { store("acc.getStarted", "hidden"); setHidden(true); }}>Hide</Button>} />
      <ol className="divide-y divide-line" aria-label="Get started steps">
        {steps.map((x, i) => (
          <li key={x.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3" data-done={x.done || undefined}>
            <span className={clsx("grid size-6 shrink-0 place-items-center rounded-full border text-[12px] tnum",
              x.done ? "border-ok/40 text-ok" : "border-line-strong text-muted")} aria-hidden="true">{x.done ? <Check size={13} /> : i + 1}</span>
            <div className="min-w-0 flex-1 basis-56">
              <div className={clsx("text-[13px] font-medium", x.done ? "text-muted line-through decoration-faint" : "text-fg")}>
                {x.title}<span className="sr-only">{x.done ? " (done)" : " (to do)"}</span></div>
              {!x.done && <div className="text-xs text-muted">{x.detail}</div>}
            </div>
            {!x.done && x.action}
          </li>
        ))}
      </ol>
      <NewProjectDialog open={creating} onClose={() => setCreating(false)} />
    </Card>
  );
}

/** One quiet line pointing at the Connect your phone guide, until the person dismisses it. The dismissal is theirs
 *  (a server preference), so it stays gone on every browser; a phone step marked done here before counts too. Inside
 *  the installed phone app the phone is already connected: never shown there. */
function PhoneBanner() {
  const client = useQueryClient();
  const markedDone = stored("acc.getStarted.phone") === "done" || installedApp();
  const preference = useQuery({ queryKey: ["phone-banner"], queryFn: () => api<{ dismissed: boolean }>("/api/preferences/phone-banner"),
    enabled: !markedDone });
  const dismiss = useMutation({
    mutationFn: () => api<{ dismissed: boolean }>("/api/preferences/phone-banner", { method: "PUT", json: { dismissed: true } }),
    onMutate: () => client.setQueryData(["phone-banner"], { dismissed: true }),
    onError: () => client.invalidateQueries({ queryKey: ["phone-banner"] }),
  });
  if (markedDone || !preference.data || preference.data.dismissed) return null;
  return (
    <aside aria-label="Connect your phone" className="flex items-center gap-2.5 rounded-[10px] border border-line bg-panel py-1.5 pl-4 pr-1.5 text-[13px] shadow-card">
      <Smartphone size={15} className="shrink-0 text-muted" aria-hidden="true" />
      <p className="min-w-0 flex-1 text-muted">
        Use Rynhult from your phone, with notifications when a session needs you. <Link to="/guide/User-Guide/Connect-Your-Phone" className="text-fg underline underline-offset-2">Connect your phone</Link>
      </p>
      <IconButton label="Dismiss" icon={X} onClick={() => dismiss.mutate()} />
    </aside>
  );
}

function ProjectsGlance() {
  const projects = useProjects();
  const sessions = useSessions();
  const host = useHost();
  const containers = host.data?.snapshot?.containers ?? [];
  const list = (projects.data ?? []).filter(usableProject);
  const [creating, setCreating] = useState(false);
  return (
    <Card>
      <NewProjectDialog open={creating} onClose={() => setCreating(false)} />
      <CardHeader title="Projects" icon={FolderGit2} action={<Link to="/projects" className="inline-flex min-h-6 pointer-coarse:min-h-11 items-center text-xs text-muted hover:text-fg underline underline-offset-2">All projects</Link>} />
      <QueryState query={projects} loading={<LoadingRows rows={4} />} errorTitle="Couldn't read the projects." isEmpty={() => !list.length}
        empty={<div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-xs text-muted">No projects yet.
          <Button size="sm" onClick={() => setCreating(true)}><Plus size={13} aria-hidden="true" /> Add project</Button></div>}>
        {() => (
        <div className="divide-y divide-line">
          {list.map((p) => {
            const mine = containers.filter((c) => c.project === p.slug && !c.status.startsWith("Exited (0)"));
            const up = mine.filter((c) => c.state === "running").length;
            const live = (sessions.data ?? []).filter((s) => s.project_id === p.id && isLive(s)).length;
            const tone = !mine.length ? "neutral" : up === mine.length ? "ok" : up ? "wait" : "bad";
            return (
              <Link key={p.id} to={`/projects/${p.id}`} className="flex pointer-coarse:min-h-11 items-center gap-3 px-4 py-2.5 text-[13px] hover:bg-panel-2/70 underline underline-offset-2">
                <Dot tone={tone} />
                <span className="min-w-0 flex-1 truncate text-fg">{p.name}</span>
                {live > 0 && <span className="tnum flex items-center gap-1 text-xs text-muted" title={`${live} live session(s)`}><TerminalSquare size={12} aria-hidden="true" />{live}</span>}
                <span className="tnum w-16 text-right text-xs text-faint">{mine.length ? `${up}/${mine.length} up` : "no stack"}</span>
              </Link>
            );
          })}
        </div>
        )}
      </QueryState>
    </Card>
  );
}

export function Overview() {
  const [hostOrder, setHostOrder] = useState<"asc" | "desc" | null>(null);
  const { host: hostLabel } = useInstance();
  const { owner } = useRole();
  const newSession = useNewSession();
  const sessions = useSessions();
  const host = useHost();
  const projects = useProjects();
  const nodes = useNodes(owner);
  const attention = useAttention();
  const inbox = sortInbox(attention.data ?? []);
  const all = (sessions.data ?? []).filter((s) => s.desired_state !== "archived");
  const live = all.filter((s) => isLive(s) || s.observed_state === "recovery_needed");
  const rest = all.filter((s) => !live.includes(s));
  const liveGroups = groupLive(live);
  const liveCounts = SESSION_GROUPS.filter((g) => ["working", "unknown", "idle"].includes(g.key))
    .map((g) => ({ ...g, count: sessionCount(live, g.key) })).filter((g) => g.count > 0);
  const h = host.data;
  const snap = h?.snapshot;
  const containers = (snap?.containers ?? []).filter((c) => !c.status.startsWith("Exited (0)"));  // finished one-shot jobs aren't "down"
  const containersUp = containers.filter((c) => c.state === "running").length;
  const failedUnits = snap?.services?.failed.length ?? 0;
  const critical = inbox.some((i) => i.severity === "critical");
  const blocked = needsYou(live);
  const activeProjects = (projects.data ?? []).filter((p) => !p.archived && p.import_mode === "managed" && !isLocalProject(p)).length;

  // Phones read top to bottom: what needs you, then the sessions to answer, then health and administration (UX
  // declutter D03). The DOM follows that order, so keyboard and reading order match it. Desktop keeps the four tiles in
  // one row, and machines join the side column. The only reordering moves the two health tiles, which hold no
  // controls, below the sessions on narrow screens.
  const urgent = blocked.length > 0 || critical;
  const machines = owner ? (nodes.data ?? []) : [];
  const onboarding = <><GetStarted /><PhoneBanner /></>;
  return (
    <Page wide>
      <PageHeader title="Overview" description={`${owner ? "Sessions" : "Your sessions"}, attention and the health of ${hostLabel} at a glance.`} />
      <div className="grid min-w-0 grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        {!urgent && <div className="order-first min-w-0 space-y-6 empty:hidden xl:col-span-2">{onboarding}</div>}
        <div className="contents lg:grid lg:grid-cols-4 lg:gap-3 xl:col-span-2">
          <div className="order-1 grid grid-cols-2 gap-3 lg:contents">
            <Stat label="Needs you" icon={TerminalSquare} value={sessions.data ? blocked.length : "–"} tone={blocked.length ? "wait" : undefined}
              href={blocked.length ? "/sessions?group=needs_you" : "/sessions"}
              detail={!sessions.data ? " " : live.length
                ? `${live.length} live${liveCounts.map((g) => ` · ${g.count} ${g.label.toLowerCase()}`).join("")}`
                : "None running"} />
            <Stat label="Inbox" icon={BellRing} value={attention.data ? inbox.length : "–"} tone={critical ? "bad" : inbox.some((i) => i.severity !== "info") ? "wait" : undefined}
              detail={!attention.data ? " " : inbox.length ? inbox[0].title : "Nothing to review"} href="/inbox" />
          </div>
          <div className="order-3 grid grid-cols-2 gap-3 lg:contents">
            <Stat label="Host CPU" icon={Cpu} value={h ? `${h.live.cpu_percent.toFixed(0)}%` : "–"} meter={h?.live.memory.percent}
              detail={h ? `Memory ${h.live.memory.percent.toFixed(0)}% · load ${h.live.load[0].toFixed(2)} · live (psutil), ${ago(new Date(host.dataUpdatedAt).toISOString())}` : " "} />
            <Stat label="Containers up" icon={Container} value={snap ? `${containersUp}/${containers.length}` : "–"}
              tone={snap && containersUp < containers.length ? "wait" : failedUnits ? "bad" : undefined}
              detail={`${activeProjects} projects${!snap?.services ? "" : ` · ${failedUnits ? `${failedUnits} failed unit(s)` : "all units healthy"}`}`} />
          </div>
        </div>

        <div className="order-2 min-w-0 space-y-6 lg:order-none">
          <Card>
            <CardHeader title="Live sessions" icon={TerminalSquare}
              action={<Link to="/sessions" className="inline-flex pointer-coarse:min-h-11 items-center gap-1 text-xs text-muted hover:text-fg underline underline-offset-2">All sessions <ArrowRight size={12} aria-hidden="true" /></Link>} />
            <QueryState query={sessions} errorTitle="Couldn't read the sessions." isEmpty={() => !live.length}
              empty={<Empty icon={TerminalSquare} action={<Button onClick={() => newSession()}><Plus size={14} aria-hidden="true" /> Start a session</Button>}>No live sessions.</Empty>}>
            {() => liveGroups.map((group, i) => (
              <section key={group.key} aria-label={group.label}>
                <div className={clsx("flex items-center gap-2 border-b border-line bg-panel-2/60 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em]",
                  group.key === "needs_you" ? "text-wait" : "text-faint")}>
                  {group.label} <span className="tnum font-normal">{group.count}</span>
                </div>
                <SessionList sessions={group.sessions} header={i === 0} hostOrder={hostOrder}
                  onHostSort={() => setHostOrder(order => order === "asc" ? "desc" : "asc")} />
              </section>
            ))}
            </QueryState>
          </Card>
          {rest.length > 0 && (
            <details className="group">
              <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 text-[13px] text-muted hover:text-fg">
                <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" /> Stopped and exited
                <span className="tnum rounded bg-panel-2 px-1.5 text-[11px]">{rest.length}</span>
              </summary>
              <Card className="mt-3"><SessionList sessions={rest} /></Card>
            </details>
          )}
          {/* While something needs you, onboarding hints wait below the work. */}
          {urgent && onboarding}
        </div>
        <div className="order-4 min-w-0 space-y-6 lg:order-none">
          {/* Each machine's state stays on Overview (an offline always-on machine stands out); checks live on Host. */}
          {(machines.length > 0 || nodes.error) && <Card>
            <CardHeader title="Machines"
              action={<Link to="/host" className="inline-flex min-h-6 pointer-coarse:min-h-11 items-center text-xs text-muted hover:text-fg underline underline-offset-2">All machines</Link>} />
            <div className="space-y-3 px-4 py-3">
              {machines.map(node => <div key={node.id} className="flex flex-wrap gap-2 text-xs"><b>{node.name}</b><span className="text-muted">{node.role === 'workstation' ? 'Workstation' : 'Always-on'}</span><MachineState node={node} /></div>)}
              <ErrorNote error={nodes.error} />
            </div>
          </Card>}
          {owner && <PlanUsage />}
          <ProjectsGlance />
          {snap && (
            <Card>
              <CardHeader title="Storage" icon={HardDrive} action={<Link to="/host" className="inline-flex min-h-6 pointer-coarse:min-h-11 items-center text-xs text-muted hover:text-fg underline underline-offset-2">Host details</Link>} />
              <div className="space-y-3 p-4">
                {snap.disks.map((d) => (
                  <div key={d.mount}>
                    <div className="mb-1.5 flex justify-between text-xs"><span className="font-mono text-fg">{d.mount}</span>
                      <span className="tnum text-muted">{d.percent.toFixed(0)}% · {(d.free / 1e9).toFixed(0)} GB free</span></div>
                    <Meter value={d.percent} label={`${d.mount} used`} />
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </Page>
  );
}
