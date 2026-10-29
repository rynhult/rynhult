import clsx from "clsx";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowLeft, Bell, FolderGit2, Gauge, Lock, Maximize, Minimize, Server, TerminalSquare, TriangleAlert } from "lucide-react";
import { api } from "../lib/api";
import { ago, duration, isLocalProject, until } from "../lib/format";
import { diskMountWords } from "../lib/words";
import { useAppliedTheme } from "../lib/theme";
import { groupInbox, INBOX_GROUPS, lastActive, needsYou, sessionCount } from "../lib/order";
import { providerShort, useAttention, useInstance, useOffline, useProviderRegistry, useRole, useSessions } from "../lib/queries";
import type { AttentionItem, BoardMachine, BoardView, Observation, SessionSummary } from "../lib/types";
import { groupLive, isLive, NowDoing, ProviderMark, SessionMachine, sessionState } from "../components/session-bits";
import { BoardPanel, BoardStat, Button, Card, Dot, Empty, ErrorNote, Meter, Note, Popover, Skeleton, useDocumentTitle } from "../components/ui";

/* The status board ("war room"): one read-only screen for a large display, 16:9, read from across a room.
   Healthy is quiet: dots and plain numbers; amber and red only where something needs someone. Everything refreshes on
   the shared queries' own intervals (sessions and the Inbox every 3 s, the board every 10 s, plan usage every 30 s). */

const useBoard = () => useQuery({ queryKey: ["board"], queryFn: () => api<BoardView>("/api/board"), refetchInterval: 10000 });
const useObservations = () => useQuery({ queryKey: ["observations"], queryFn: () => api<Observation[]>("/api/observations"), refetchInterval: 30000 });
const BACKUP_STALE = 2 * 86400e3;  // a project's last good backup older than this is amber
/** Running: as the sidebar's live list (a session waiting for recovery counts; archived ones don't). */
const running = (s: SessionSummary) => s.desired_state !== "archived" && (isLive(s) || s.observed_state === "recovery_needed");
const ROW = "px-4 py-2.5 2xl:px-5 2xl:py-3";
const TEXT = "text-[14px] 2xl:text-[17px]";
const SMALL = "text-[12.5px] 2xl:text-[15px]";

/** A second hand, so durations and "updated" ages move between refreshes. */
function useNow(every = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), every); return () => clearInterval(timer); }, [every]);
  return now;
}

function Usage({ label, value, mount }: { label: string; value: number | null | undefined; mount?: string }) {
  return (
    <div className="min-w-0">
      <div className={clsx("flex justify-between gap-2", SMALL)}>
        <span className="truncate text-muted">{label}{mount ? <span className="font-mono text-faint" title={mount}> {diskMountWords(mount)}</span> : null}</span>
        <span className={clsx("tnum", value == null ? "text-faint" : value > 85 ? "font-semibold text-bad" : value > 60 ? "text-wait" : "text-fg")}>{value == null ? "–" : `${Math.round(value)}%`}</span>
      </div>
      {value != null && <Meter value={value} className="mt-1" label={`${label}${mount ? ` ${diskMountWords(mount)}` : ""} used`} />}
    </div>
  );
}

function MachineRow({ m }: { m: BoardMachine }) {
  const planned = m.planned_downtime?.active;
  const problem = (!m.connected && !planned) || m.alerts.some((a) => a.severity !== "info");
  const critical = (!m.connected && !planned) || m.alerts.some((a) => a.severity === "critical");
  return (
    <li className={clsx(ROW, "border-l-2", problem ? (critical ? "border-l-bad" : "border-l-wait") : "border-l-transparent")} data-machine={m.name}>
      <div className={clsx("flex flex-wrap items-baseline gap-x-2.5", TEXT)}>
        <Dot large tone={!m.connected && planned ? "neutral" : !m.connected ? "bad" : critical ? "bad" : problem ? "wait" : "ok"} className="self-center" />
        <span className="font-semibold text-fg">{m.label}</span>
        {m.local && <span className={clsx("text-faint", SMALL)}>main</span>}
        <span className="sr-only">{planned ? "Planned downtime" : m.connected ? "connected" : "disconnected"}</span>
        <span className={clsx("ml-auto text-faint", SMALL)}>{m.version ? `Rynhult ${m.version}` : "version unknown"}{m.update_available && m.version ? " · update available" : ""}</span>
      </div>
      {!m.connected ? (
        <p className={clsx("mt-1 pl-5", planned ? "text-muted" : "font-medium text-bad", SMALL)}>{planned ? "Planned downtime" : "Disconnected"}{m.last_seen ? ` · last seen ${ago(m.last_seen)}` : ""}</p>
      ) : !m.supported ? (
        <p className={clsx("mt-1 pl-5 text-faint", SMALL)}>Update this machine to see its numbers here</p>
      ) : !m.light ? (
        <p className={clsx("mt-1 pl-5 text-faint", SMALL)}>Waiting for its first summary</p>
      ) : (
        <div className="mt-1.5 grid grid-cols-3 gap-3 pl-5">
          <Usage label="CPU" value={m.light.cpu_percent} />
          <Usage label="Memory" value={m.light.memory_percent} />
          <Usage label="Disk" value={m.light.disk?.percent} mount={m.light.disk?.mount} />
        </div>
      )}
      {m.connected && m.stale && m.light_at && <p className={clsx("mt-1 pl-5 text-wait", SMALL)}>Numbers from {ago(new Date(m.light_at * 1000).toISOString())}</p>}
      {m.alerts.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 pl-5">
          {m.alerts.map((a) => (
            <li key={a.title} className={clsx("flex items-center gap-1.5", SMALL, a.severity === "critical" ? "font-medium text-bad" : a.severity === "warn" ? "text-wait" : "text-muted")}>
              <TriangleAlert size={13} aria-hidden="true" className="shrink-0" />{a.title}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Machines({ board }: { board?: BoardView }) {
  const machines = board?.machines ?? [];
  const connected = machines.filter((m) => m.connected).length;
  return (
    <BoardPanel title="Machines" icon={Server} className="lg:col-span-4 lg:col-start-1 lg:row-start-1"
      summary={board ? <span className={machines.some(m => !m.connected && !m.planned_downtime?.active) ? "font-medium text-bad" : undefined}>{connected} of {machines.length} connected</span> : undefined}>
      {!board ? <div className="p-4"><Skeleton className="h-24" /></div>
        : <ul className="divide-y divide-line">{machines.map((m) => <MachineRow key={m.name} m={m} />)}</ul>}
    </BoardPanel>
  );
}

function backupWords(p: BoardView["projects"][number], now: number): { text: string; tone?: "wait" | "bad" } {
  if (!p.git_backup) return { text: "off" };
  const b = p.backup;
  if (!b || (!b.last_at && !b.failed_at)) return { text: "not yet", tone: "wait" };
  if (b.failed_at && (!b.last_at || Date.parse(b.failed_at) > Date.parse(b.last_at))) return { text: `failed ${ago(b.failed_at)}`, tone: "bad" };
  return { text: ago(b.last_at), tone: b.last_at && now - Date.parse(b.last_at) > BACKUP_STALE ? "wait" : undefined };
}

function Projects({ board, sessions, now }: { board?: BoardView; sessions: SessionSummary[]; now: number }) {
  const rows = (board?.projects ?? []).filter((p) => !isLocalProject(p)).map((p) => {
    const mine = sessions.filter((s) => s.project_id === p.id);
    const live = mine.filter(running);
    const last = Math.max(0, ...mine.map(lastActive));
    return { ...p, live: live.length, needs: needsYou(live).length, last };
  }).sort((a, b) => b.live - a.live || b.last - a.last);
  const machine = (host: string) => (host === "local" ? board?.machines.find((m) => m.local)?.label ?? "main" : host);
  return (
    <BoardPanel title="Projects" icon={FolderGit2} className="lg:col-span-4 lg:col-start-1 lg:row-start-2" summary={board ? `${rows.length}` : undefined}>
      {!board ? <div className="p-4"><Skeleton className="h-24" /></div> : !rows.length ? <Empty icon={FolderGit2}>No projects yet.</Empty> : (
        <ul className="divide-y divide-line">
          {rows.map((p) => {
            const backup = backupWords(p, now);
            return (
              <li key={p.id} className={clsx(ROW, "border-l-2", backup.tone === "bad" ? "border-l-bad" : "border-l-transparent")} data-project={p.slug}>
                <div className={clsx("flex items-baseline gap-2.5", TEXT)}>
                  <span className="min-w-0 truncate font-semibold text-fg" title={p.name}>{p.name}</span>
                  <span className={clsx("shrink-0 text-faint", SMALL)}>{machine(p.host)}</span>
                  <span className={clsx("tnum ml-auto shrink-0", SMALL)}>{p.live
                    ? <span className="text-fg">{p.live} live{p.needs ? <span className="font-semibold text-wait"> · {p.needs} need{p.needs === 1 ? "s" : ""} you</span> : null}</span>
                    : <span className="text-faint">none live</span>}</span>
                </div>
                <div className={clsx("tnum mt-0.5 flex flex-wrap gap-x-3 text-muted", SMALL)}>
                  <span>Active {p.last ? ago(new Date(p.last).toISOString()) : "never"}</span>
                  <span className={backup.tone === "bad" ? "font-medium text-bad" : backup.tone === "wait" ? "text-wait" : undefined}>Backup {backup.text}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </BoardPanel>
  );
}

function SessionRow({ s, now }: { s: SessionSummary; now: number }) {
  const registry = useProviderRegistry();
  const state = sessionState(s);
  const needs = state.tone === "wait" || state.tone === "bad";
  return (
    <li className={clsx(ROW, "border-l-2", needs ? (state.tone === "bad" ? "border-l-bad" : "border-l-wait") : "border-l-transparent")} data-session={s.name}>
      <div className={clsx("flex items-baseline gap-2.5", TEXT)}>
        <Dot large tone={state.tone} pulse={state.pulse} className="self-center" />
        <span className="min-w-0 truncate font-semibold text-fg">{s.name}</span>
        <span className={clsx("min-w-0 truncate text-muted", SMALL)}>{s.project}</span>
        <span className={clsx("ml-auto shrink-0", needs ? "font-semibold" : "", state.text, SMALL)}>{state.label}</span>
      </div>
      <div className={clsx("mt-0.5 flex items-center gap-2.5 pl-5 text-muted", SMALL)}>
        <span className="inline-flex shrink-0 items-center gap-1.5"><ProviderMark provider={s.provider} />{providerShort(s.provider, registry.byId(s.provider))}</span>
        <span className="tnum shrink-0 text-faint" title="How long it has been running">{duration(s.created_at, now)}</span>
        <SessionMachine session={s} inline />
        <NowDoing session={s} className="min-w-0 truncate" />
      </div>
    </li>
  );
}

function Sessions({ sessions, loaded, now }: { sessions: SessionSummary[]; loaded: boolean; now: number }) {
  const live = sessions.filter(running);
  const groups = groupLive(live);
  return (
    <BoardPanel title="Running sessions" icon={TerminalSquare} className="lg:col-span-5 lg:col-start-5 lg:row-span-2 lg:row-start-1" summary={loaded ? `${live.length}` : undefined}>
      {!loaded ? <div className="p-4"><Skeleton className="h-40" /></div> : !live.length ? <Empty icon={TerminalSquare}>No sessions running.</Empty> : groups.map((g) => (
        <div key={g.key}>
          <h3 className={clsx("border-b border-line bg-panel-2 px-4 py-1.5 font-medium uppercase tracking-[0.06em] 2xl:px-5", "text-[11px] 2xl:text-[13px]", g.key === "needs_you" ? "text-wait" : "text-muted")}>
            {g.label} · <span className="tnum">{g.count}</span></h3>
          <ul className="divide-y divide-line">{g.sessions.map((s) => <SessionRow key={s.id} s={s} now={now} />)}</ul>
        </div>
      ))}
    </BoardPanel>
  );
}

type Window = { label: string; pct: number; resets?: number };
/** Plan usage per provider, as each CLI reports it (never combined), with its age. */
function planRows(observations: Observation[]) {
  const get = (key: string) => observations.find((o) => o.key === key);
  const rows: { provider: string; label: string; observed_at: number; windows: Window[] }[] = [];
  const claude = get("claude.rate_limits");
  if (claude) rows.push({ provider: "claude", label: "Claude", observed_at: claude.observed_at, windows: ([["five_hour", "5-hour"], ["seven_day", "7-day"]] as const)
    .filter(([k]) => claude.value[k]).map(([k, label]) => ({ label, pct: Math.round(claude.value[k].used_percentage ?? 0), resets: claude.value[k].resets_at })) });
  const codex = get("codex.rate_limits");
  if (codex?.value?.primary) rows.push({ provider: "codex", label: "Codex", observed_at: codex.observed_at, windows: (["primary", "secondary"] as const)
    .filter((k) => codex.value[k]).map((k) => ({ label: codex.value[k].window_minutes >= 1440 ? "Weekly" : "5-hour", pct: Math.round(codex.value[k].used_percent ?? 0), resets: codex.value[k].resets_at })) });
  const copilot = get("copilot.usage");
  const credits = copilot?.value?.credits;
  if (copilot && credits && !credits.unlimited) rows.push({ provider: "copilot", label: "Copilot", observed_at: copilot.observed_at,
    windows: [{ label: "AI credits", pct: Math.round(100 - (credits.percent_remaining ?? 100)), resets: copilot.value.resets_at ? Date.parse(copilot.value.resets_at) / 1000 : undefined }] });
  return rows;
}

function SourceStatus({ name, query }: { name: string; query: { error: unknown; dataUpdatedAt: number; refetch: () => unknown } }) {
  if (!query.error) return null;
  return <Note role="status"><span className="flex flex-wrap items-center gap-2">
    <span>{name}: refresh failed. {query.dataUpdatedAt ? `Showing stale data; last successful update ${new Date(query.dataUpdatedAt).toLocaleString()}.` : 'No data loaded.'}</span>
    <Button size="sm" onClick={() => query.refetch()}>Retry<span className="sr-only"> {name}</span></Button>
  </span></Note>;
}

function PlanUsage() {
  const observations = useObservations();
  const rows = planRows(observations.data ?? []);
  return (
    <BoardPanel title="Plan usage" icon={Gauge} className="lg:flex-1"><SourceStatus name="Plan usage" query={observations} />
      {!observations.data ? (observations.error ? <div className="p-4"><ErrorNote error={observations.error} /></div> : <div className="p-4"><Skeleton className="h-20" /></div>)
        : !rows.length ? <p className={clsx("p-4 text-faint", SMALL)}>No readings yet: they appear after a session's first reply.</p> : (
        <ul className="divide-y divide-line">
          {rows.map((r) => (
            <li key={r.provider} className={ROW}>
              <div className={clsx("flex items-baseline justify-between gap-2", SMALL)}>
                <span className={clsx("inline-flex items-center gap-1.5 font-semibold text-fg", TEXT)}><ProviderMark provider={r.provider} />{r.label}</span>
                <span className="text-faint">{ago(new Date(r.observed_at * 1000).toISOString())}</span>
              </div>
              <div className="mt-1.5 space-y-1.5">
                {r.windows.map((w) => (
                  <div key={w.label}>
                    <div className={clsx("flex justify-between gap-2", SMALL)}>
                      <span className="text-muted">{w.label}{w.resets ? <span className="text-faint"> · resets {until(w.resets)}</span> : null}</span>
                      <span className={clsx("tnum", w.pct > 85 ? "font-semibold text-bad" : w.pct > 60 ? "text-wait" : "text-fg")}>{w.pct}%</span>
                    </div>
                    <Meter value={w.pct} className="mt-1" label={`${r.label} ${w.label} used`} />
                  </div>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
    </BoardPanel>
  );
}

function InboxCounts({ items }: { items?: AttentionItem[] }) {
  const groups = groupInbox(items ?? []);
  const count = (key: string) => groups.find((g) => g.key === key)?.items.length ?? 0;
  return (
    <BoardPanel title="Inbox" icon={Bell} className="shrink-0" summary={items ? `${items.length} open` : undefined}>
      {!items ? <div className="p-4"><Skeleton className="h-12" /></div> : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-4 py-3 2xl:gap-y-3 2xl:px-5 2xl:py-4">
          {INBOX_GROUPS.map((g) => {
            const n = count(g.key);
            const tone = !n || g.key === "notes" ? undefined : g.key === "alerts" && groups.find((x) => x.key === "alerts")?.items.some((i) => i.severity === "critical") ? "bad" : "wait";
            return (
              <div key={g.key}>
                <dt className={clsx("text-muted", SMALL)}>{g.label}</dt>
                <dd className={clsx("tnum text-[22px] font-semibold leading-tight 2xl:text-[28px]", tone === "bad" ? "text-bad" : tone === "wait" ? "text-wait" : n ? "text-fg" : "text-faint")}>{n}</dd>
              </div>
            );
          })}
        </dl>
      )}
    </BoardPanel>
  );
}

/** Open Inbox alerts and other machines' pressure alerts (this machine's are Inbox items already), newest first. */
function RecentAlerts({ items, machines }: { items?: AttentionItem[]; machines: BoardMachine[] }) {
  const fromMachines = machines.filter((m) => !m.local && m.connected).flatMap((m) => m.alerts.filter((a) => a.severity !== "info")
    .map((a) => ({ key: `${m.name}:${a.title}`, severity: a.severity, title: a.title, where: m.name, at: m.light_at ? m.light_at * 1000 : Date.now() })));
  const fromInbox = (items ?? []).filter((i) => groupInbox([i])[0]?.key === "alerts")
    .map((i) => ({ key: String(i.id), severity: i.severity, title: i.title, where: i.project ?? i.session_name, at: Date.parse(i.updated_at) }));
  const alerts = [...fromInbox, ...fromMachines].sort((a, b) => b.at - a.at).slice(0, 8);
  return (
    <BoardPanel title="Recent alerts" icon={TriangleAlert} className="flex-1" summary={items ? `${alerts.length}` : undefined}>
      {!items ? <div className="p-4"><Skeleton className="h-20" /></div> : !alerts.length ? <p className={clsx("p-4 text-faint", SMALL)}>No open alerts.</p> : (
        <ul className="divide-y divide-line">
          {alerts.map((a) => (
            <li key={a.key} className={ROW}>
              <div className={clsx("flex items-baseline gap-2", SMALL)}>
                <Dot large tone={a.severity === "critical" ? "bad" : "wait"} className="self-center" />
                <span className={clsx("min-w-0 flex-1", a.severity === "critical" ? "font-semibold text-bad" : "text-fg")}>{a.title}</span>
              </div>
              <div className={clsx("pl-5 text-faint", SMALL)}>{[a.where, ago(new Date(a.at).toISOString())].filter(Boolean).join(" · ")}</div>
            </li>
          ))}
        </ul>
      )}
    </BoardPanel>
  );
}

function FullScreen() {
  const [on, setOn] = useState(() => !!document.fullscreenElement);
  useEffect(() => {
    const change = () => setOn(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, []);
  if (!document.fullscreenEnabled) return null;
  const Icon = on ? Minimize : Maximize;
  return (
    <button type="button" onClick={() => (on ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => undefined)}
      className="inline-flex h-11 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2 lg:h-9">
      <Icon size={15} aria-hidden="true" />{on ? "Exit full screen" : "Full screen"}</button>
  );
}

export function Board() {
  useAppliedTheme();
  useDocumentTitle("Status board");
  const { owner, known } = useRole();
  if (known && !owner) {
    return (
      <main className="grid min-h-full place-items-center bg-bg p-4">
        <Card className="max-w-md"><Empty icon={Lock} action={<Link to="/" className="text-[13px] font-medium text-accent underline-offset-2 hover:underline">Back to Rynhult</Link>}>
          The status board is for Rynhult's owners.</Empty></Card>
      </main>
    );
  }
  return <BoardScreen />;
}

function BoardScreen() {
  const instance = useInstance();
  const now = useNow();
  const board = useBoard();
  const sessions = useSessions();
  const attention = useAttention();
  const offline = useOffline();
  const list = sessions.data ?? [];
  const live = list.filter(running);
  const needs = needsYou(live).length;
  const working = sessionCount(live, "working");
  const machines = board.data?.machines ?? [];
  const down = machines.filter((m) => !m.connected).length;
  const plannedDown = machines.filter(m => !m.connected && m.planned_downtime?.active).length;
  const machineAlerts = machines.reduce((n, m) => n + m.alerts.filter((a) => a.severity !== "info").length, 0);
  const updated = board.dataUpdatedAt;
  const freshness = (
    <p className={clsx("flex items-center gap-2 text-muted", SMALL)} role="status">
      {offline ? <><Dot tone="bad" /><span className="font-medium text-bad">Offline: showing data from {updated ? ago(new Date(updated).toISOString()) : "earlier"}</span></>
        : <><Dot tone={board.error || sessions.error || attention.error ? "wait" : "ok"} pulse={!board.error} /><span>{instance.host} · machines/projects {board.error ? "stale" : "updated"} <span className="tnum">{updated ? Math.max(0, Math.round((now - updated) / 1000)) : 0}s</span> ago</span></>}
    </p>
  );
  const secondaryStats = <>
    <BoardStat label="Machines" value={board.data ? `${machines.length - down}/${machines.length}` : "–"} tone={down > plannedDown ? "bad" : undefined} detail={down ? [down > plannedDown ? `${down - plannedDown} disconnected` : "", plannedDown ? `${plannedDown} planned downtime` : ""].filter(Boolean).join(" · ") : "connected"} />
    <BoardStat label="Machine alerts" value={board.data ? machineAlerts : "–"} tone={machineAlerts ? "wait" : undefined} />
    <BoardStat label="Inbox" value={attention.data ? attention.data.length : "–"} />
  </>;
  const utilities = <>
    <span className={clsx("tnum hidden text-muted md:inline", TEXT)} aria-hidden="true">{new Date(now).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
    <FullScreen />
    <Link to="/" className="inline-flex h-11 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2 lg:h-9">
      <ArrowLeft size={15} aria-hidden="true" />Back to Rynhult</Link>
  </>;
  return (
    <div className="flex min-h-full flex-col bg-bg text-fg lg:h-full lg:overflow-hidden">
      <header className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-8 gap-y-1 border-b border-line px-4 py-3 md:px-6 lg:flex lg:flex-wrap lg:gap-y-3 2xl:px-8 2xl:py-4">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-[-0.01em] 2xl:text-[26px]">Status board</h1>
          <div className="hidden lg:block">{freshness}</div>
        </div>
        <div className="order-first col-span-2 grid grid-cols-2 items-center gap-x-8 gap-y-2 lg:order-none lg:flex lg:flex-1 lg:flex-wrap 2xl:gap-x-12">
          <BoardStat label="Needs you" value={sessions.data ? needs : "–"} tone={needs ? "wait" : undefined} />
          <BoardStat label="Working" value={sessions.data ? working : "–"} />
          <div className="hidden lg:contents">{secondaryStats}</div>
        </div>
        <div className="hidden items-center gap-2 lg:flex">{utilities}</div>
        <Popover label="Board details" trigger="Details" wide className="lg:hidden [&>button]:min-h-11 [&>button]:min-w-11">
          <div className="space-y-3 p-2 [overflow-wrap:anywhere]">
            <div className="grid grid-cols-2 gap-3">{secondaryStats}</div>
            {freshness}
            <div className="flex flex-wrap items-center gap-2">{utilities}</div>
          </div>
        </Popover>
        {offline && <div className="col-span-2 lg:hidden">{freshness}</div>}
      </header>
      <SourceStatus name="Machines and projects" query={board} /><SourceStatus name="Sessions" query={sessions} /><SourceStatus name="Inbox and recent alerts" query={attention} />
      {board.error && !board.data ? <div className="p-4"><ErrorNote error={board.error} onRetry={() => board.refetch()} /></div> : null}
      <main className="grid min-w-0 flex-1 grid-cols-1 gap-3 p-3 [overflow-wrap:anywhere] md:p-4 lg:min-h-0 lg:grid-cols-12 lg:grid-rows-2 2xl:gap-4 2xl:p-5">
        <Machines board={board.data} />
        <Projects board={board.data} sessions={list} now={now} />
        <Sessions sessions={list} loaded={!!sessions.data} now={now} />
        <div className="flex min-h-0 flex-col gap-3 lg:col-span-3 lg:col-start-10 lg:row-start-1 2xl:gap-4">
          <PlanUsage />
          <InboxCounts items={attention.data} />
        </div>
        <div className="flex min-h-0 flex-col lg:col-span-3 lg:col-start-10 lg:row-start-2">
          <RecentAlerts items={attention.data} machines={machines} />
        </div>
      </main>
    </div>
  );
}
