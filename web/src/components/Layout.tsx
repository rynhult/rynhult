import { UnsavedBriefGuard } from './UnsavedBriefGuard';
import { desktopBuild } from "../lib/desktop";
import { ago, teamOrder, withTeamAncestors, workspaceName } from "../lib/format";
import clsx from "clsx";
import { ChevronRight, CornerDownRight, ExternalLink, LogOut, Menu, Monitor, Moon, Plus, Sun, X } from "lucide-react";
import { NEXT_THEME, readTheme, saveTheme, THEME_LABEL, useAppliedTheme, type ThemeChoice } from "../lib/theme";
import { lazy, Suspense, type ReactNode, useCallback, useEffect, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, post } from "../lib/api";
import { BROKER_DOWN_TITLE, useAttention, useBrokerDown, useHealth, useOffline, useInstance, useProjects, useGroupAdministration, useRole, useSessions } from "../lib/queries";
import { useUpdates } from "./Updates";
import { ErrorBoundary } from "./ErrorBoundary";
import { CommandPalette } from "./CommandPalette";
import { navFor, PHONE_TABS } from "./nav";
import { lazyImport } from "../lib/lazyImport";
import { NewSessionContext, type NewSessionPreset } from "./newSession";
import { groupLive, isLive, nowDoing, sessionState } from "./session-bits";
import { announce, Button, Dialog, Dot, ErrorNote, IconButton, Kbd, useMedia } from "./ui";
import type { AttentionItem } from "../lib/types";

const NewSessionDialog = lazy(() => lazyImport(() => import("./NewSessionDialog").then(m => ({ default: m.NewSessionDialog }))));

/** System, Light or Dark; System follows the device, including when it changes while Rynhult is open. */
function useTheme() {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);
  useAppliedTheme(choice);
  const toggle = useCallback(() => setChoice((current) => { const next = NEXT_THEME[current]; saveTheme(next); return next; }), []);
  return { choice, toggle };
}

/** Live sessions pinned per sidebar group before "+N more" (owner decision 0008, Q3). */
const PINNED = 5;

/** Amber while something waits on you, red when something is broken, quiet for FYI notes only. */
const inboxBadgeTone = (items: AttentionItem[]) =>
  items.some((i) => i.severity === "critical") ? "bg-bad text-status-ink" : items.some((i) => i.severity !== "info") ? "bg-wait text-status-ink" : "bg-panel-3 text-fg";

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** Breadcrumb trail for the top bar, from the route and the cached sessions/projects. */
function useCrumbs(): { label: string; to?: string }[] {
  const nav = navFor(useRole().owner, useGroupAdministration(), useInstance().features);
  const { pathname } = useLocation();
  const sessions = useSessions();
  const projects = useProjects();
  const item = nav.flatMap((g) => g.items).find((n) => (n.end ? pathname === n.to : pathname.startsWith(n.to) && n.to !== "/"));
  const [, section, id] = pathname.split("/");
  if (section === "multiruns" && id) return [{ label: "Sessions", to: "/sessions" }, { label: "Multi-run" }];
  if (section === "sessions" && id) {
    const s = sessions.data?.find((x) => x.id === id);
    return [{ label: "Sessions", to: "/sessions" }, { label: s?.name ?? id }];
  }
  if (section === "projects" && id) {
    const p = projects.data?.find((x) => String(x.id) === id);
    const wiki = pathname.split("/")[3] === "wiki";
    return [{ label: "Projects", to: "/projects" }, { label: p?.name ?? "Project", to: wiki ? `/projects/${id}` : undefined },
      ...(wiki ? [{ label: "Wiki" }] : [])];
  }
  if (section === "workspaces" && id) return [{ label: "Workspaces", to: "/workspaces" }, { label: workspaceName(id) }, { label: pathname.split("/")[3] === "files" ? "Files" : "Wiki" }];
  return [{ label: item?.label ?? "Overview" }];
}

function Sidebar({ username, onTheme, theme, onLogout, onNavigate }: {
  username: string; onTheme: () => void; theme: ThemeChoice; onLogout: () => void; onNavigate?: () => void;
}) {
  const owner = useRole().owner;
  const updates = useUpdates(owner);
  const nav = navFor(owner, useGroupAdministration(), useInstance().features);
  const attention = useAttention();
  const sessions = useSessions();
  const health = useHealth();
  const open = attention.data ?? [];  // every open Inbox item, FYI notes too (owner decision 0008, Q1)
  const live = (sessions.data ?? []).filter((s) => s.desired_state !== "archived" && (isLive(s) || s.observed_state === "recovery_needed"));
  const treeSessions = withTeamAncestors(sessions.data ?? [], live);
  const brokerDown = useBrokerDown();
  const brokerOk = brokerDown ? false : health.data?.broker === false ? undefined : health.data?.broker;
  const instance = useInstance();
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 shrink-0 items-center gap-2.5 px-4">
        <div className="grid size-7 shrink-0 place-items-center" aria-hidden="true">
          <img src="/rynhult-mark-light.svg" alt="" className="hidden w-1/2 dark:block" />
          <img src="/rynhult-mark-dark.svg" alt="" className="w-1/2 dark:hidden" />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="text-[13px] font-semibold">Rynhult</div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted" title={brokerOk === false ? BROKER_DOWN_TITLE : brokerOk ? "Agent sessions service connected" : "Checking the agent sessions service"}>
            <Dot tone={brokerOk === false ? "bad" : brokerOk ? "ok" : "neutral"} className="size-1.5 [&>span]:size-1.5" /> {instance.host}
          </div>
        </div>
      </div>
      {owner && updates.data?.available && <Link to="/settings#updates" className="px-4 pb-3 text-xs text-wait underline underline-offset-2" onClick={onNavigate}>Update available</Link>}
      {/* Pinned live sessions scroll on their own, so Operate and Configure stay in view however many are live (UX review
          NAV-2); the whole column scrolls only when the window is too short for both. */}
      <nav className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-2.5 pb-4 pt-1" aria-label="Main">
        {nav.map((group, gi) => (
          <div key={gi} className={gi === 0 && live.length > 0 ? "flex min-h-28 shrink flex-col" : "shrink-0"}>
            {group.label && <div className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{group.label}</div>}
            <div className="space-y-px">
              {group.items.map(({ to, label, icon: Icon, end }) => (
                <NavLink key={to} to={to} end={end ?? false} onClick={onNavigate} className={({ isActive }) => clsx(
                  "flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] transition-colors",
                  isActive ? "bg-panel-3 font-medium text-fg" : "text-muted hover:bg-panel-2 hover:text-fg")}>
                  <Icon size={15} aria-hidden="true" /> <span className="flex-1">{label}</span>
                  {to === "/inbox" && open.length > 0 && (
                    <span className={clsx("tnum min-w-5 rounded-full px-1.5 text-center text-[11px] font-semibold leading-5", inboxBadgeTone(open))}
                      aria-label={`${open.length} open`}>{open.length}</span>
                  )}
                  {to === "/sessions" && live.length > 0 && <span className="tnum text-[11px] text-muted">{live.length}</span>}
                </NavLink>
              ))}
            </div>
            {gi === 0 && live.length > 0 && (
              <div className="-mx-1 mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto px-1" role="group" aria-label="Live sessions">
                {groupLive(treeSessions).map((group) => (
                <div key={group.key} className="space-y-px">
                <Link to={`/sessions?group=${group.key}`} onClick={onNavigate} title={`All sessions: ${group.label.toLowerCase()}`}
                  className={clsx("flex min-h-6 items-center justify-between rounded-md px-2 pb-1 text-[11px] font-semibold uppercase tracking-[0.08em] hover:text-fg",
                  group.key === "needs_you" ? "text-wait" : "text-faint")}>
                  <span>{group.label}</span><span className="tnum font-normal">{group.count}</span>
                </Link>
                {teamOrder(group.sessions).slice(0, group.sessions.some((s) => s.team_id) ? undefined : PINNED).map((s) => {
                  const st = sessionState(s);
                  if (s.management === "external" && s.remote_url) {
                    // External sessions (root's admin conversation) open in Claude Remote Control: no terminal through Rynhult.
                    return (
                      <a key={s.id} href={s.remote_url} target="_blank" rel="noreferrer" onClick={onNavigate}
                        title={`${s.name} · ${st.label} · opens in Claude Remote Control`}
                        className="flex h-7 items-center gap-2.5 rounded-md px-2 text-[12.5px] text-muted transition-colors hover:bg-panel-2 hover:text-fg">
                        <span className="grid w-[15px] place-items-center"><Dot tone={st.tone} pulse={st.pulse} /></span>
                        <span className="truncate" style={{ paddingInlineStart: `${(s.team_depth ?? 0) * 12}px` }}>{s.parent_session_id ? "↳ " : ""}{s.name}</span>
                        <ExternalLink size={12} className="ml-auto shrink-0 text-faint" aria-label="opens in Remote Control" />
                      </a>
                    );
                  }
                  return (
                    <NavLink key={s.id} to={`/sessions/${s.id}`} onClick={onNavigate} title={`${s.name} · ${s.project} · ${st.label}${nowDoing(s) ? ` · ${nowDoing(s)}` : ""}`}
                      className={({ isActive }) => clsx("flex h-7 items-center gap-2.5 rounded-md px-2 text-[12.5px] transition-colors",
                        isActive ? "bg-panel-3 text-fg" : "text-muted hover:bg-panel-2 hover:text-fg")}>
                      <span className="grid w-[15px] place-items-center"><Dot tone={st.tone} pulse={st.pulse} /></span>
                      <span className="truncate" style={{ paddingInlineStart: `${(s.team_depth ?? 0) * 12}px` }}>{s.parent_session_id ? "↳ " : ""}{s.name}</span>
                      <span className="ml-auto truncate text-[11px] text-muted">{s.project}</span>
                    </NavLink>
                  );
                })}
                {!group.sessions.some((s) => s.team_id) && group.sessions.length > PINNED && (
                  <Link to={`/sessions?group=${group.key}`} onClick={onNavigate} className="block min-h-6 px-2 py-0.5 text-[11.5px] text-faint hover:text-fg">
                    +{group.sessions.length - PINNED} more
                  </Link>
                )}
                </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </nav>
      <div className="flex h-12 shrink-0 items-center justify-between border-t border-line px-3 text-xs text-muted">
        <span className="flex min-w-0 items-center gap-2">
          <span className="grid size-6 place-items-center rounded-full bg-panel-3 text-[11px] font-semibold uppercase text-fg">{username.slice(0, 1)}</span>
          <span className="truncate">{username}</span>
        </span>
        <span className="flex">
          <IconButton label={`Theme: ${THEME_LABEL[theme]}. Select for ${THEME_LABEL[NEXT_THEME[theme]]}`}
            icon={theme === "system" ? Monitor : theme === "light" ? Sun : Moon} onClick={onTheme} />
          {!desktopBuild && <IconButton label="Sign out" icon={LogOut} onClick={onLogout} />}
        </span>
      </div>
    </div>
  );
}

/** Global freshness: every page polls; say plainly when the data has stopped updating. */
function LiveIndicator() {
  const health = useHealth();
  const offline = useOffline();
  const brokerDown = useBrokerDown();
  if (health.data?.ha) return <Link to="/ha" className="text-xs text-muted">Active main · epoch {health.data.ha.epoch ?? "unavailable"}</Link>;
  const label = offline ? "Offline, retrying" : brokerDown ? "Sessions unavailable" : "Live";
  const body = <><Dot tone={offline || brokerDown ? "bad" : "ok"} pulse={!offline && !brokerDown} /> {label}</>;
  const className = clsx("hidden items-center gap-1.5 text-xs sm:inline-flex", offline || brokerDown ? "text-bad" : "text-faint");
  // While sessions are unavailable, the indicator links to what to do.
  return brokerDown && !offline
    ? <span role="status" className="hidden sm:inline-flex"><Link to="/guide/User-Guide/Troubleshooting" title={BROKER_DOWN_TITLE} className={clsx(className, "underline underline-offset-2")}>{body}</Link></span>
    : <span role="status" title={offline ? "Rynhult is not answering; showing the last data received" : "Updating every few seconds"} className={className}>{body}</span>;
}

/** On phones the top bar has no room for the Live indicator, so a problem shows as a banner instead: hide a healthy
    indicator when space is short, never an unhealthy one (UX review NAV-4). */
function ConnectionBanner() {
  const sessions = useSessions();
  const health = useHealth();
  const offline = useOffline();
  const brokerDown = useBrokerDown();
  if (!offline && !brokerDown) return null;
  const updated = Math.max(sessions.dataUpdatedAt, offline ? 0 : health.dataUpdatedAt);
  return (
    <p role="status" className="flex shrink-0 flex-wrap items-center gap-x-1.5 border-b border-bad/30 bg-bad/10 px-4 py-1.5 text-xs text-bad sm:hidden">
      <Dot tone="bad" /> <b className="font-medium">{offline ? "Offline" : "Sessions unavailable"}</b>
      <span title={offline ? "Rynhult is not answering; showing the last data received" : BROKER_DOWN_TITLE}>
        · retrying{updated ? ` · updated ${ago(new Date(updated).toISOString())}` : ""}</span>
      {!offline && <Link to="/guide/User-Guide/Troubleshooting" className="underline underline-offset-2">What to do</Link>}
    </p>
  );
}

function InboxStatus({ hidden, onHide }: { hidden: boolean; onHide: () => void }) {
  const attention = useAttention();
  const [lastCheckFailed, setLastCheckFailed] = useState(false);
  useEffect(() => {
    if (attention.isError) setLastCheckFailed(true);
    else if (attention.isSuccess) setLastCheckFailed(false);
  }, [attention.isError, attention.isSuccess]);
  // With no cached data, a pending retry clears the query error before recovery is known.
  const unavailable = attention.isError || (lastCheckFailed && !attention.isSuccess);
  useEffect(() => { if (unavailable) announce("Inbox unavailable. Notice counts may be out of date."); }, [unavailable]);
  const items = attention.data ?? [];
  const labels = { machine_offline: "Machine offline", backup_failed: "Backup failed", backup_stale: "Backup stale" };
  const other = items.filter((item) => !item.persistent).length;
  const persistent = items.filter((item) => item.persistent);
  if (!unavailable && !persistent.length && (hidden || !other)) return null;
  return (
    <section aria-label="Inbox status" className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-panel px-4 py-1 text-xs">
      {unavailable && <>
        <span className="text-wait">Inbox unavailable</span>
        <Button size="sm" variant="ghost" disabled={attention.isFetching} onClick={() => void attention.refetch()}>Retry Inbox</Button>
      </>}
      {Object.entries(labels).map(([reason, label]) => {
        const count = persistent.filter((item) => item.persistent === reason).length;
        return count ? <Link key={reason} to="/inbox" className="whitespace-nowrap text-wait underline underline-offset-2">{label}: {count}</Link> : null;
      })}
      {!hidden && other > 0 && <span className="text-muted">{other} other {other === 1 ? "notice" : "notices"}</span>}
      <Link to="/inbox" className="inline-flex pointer-coarse:min-h-11 items-center text-fg underline underline-offset-2">{attention.data === undefined ? "Open Inbox" : `Inbox (${items.length})`}{unavailable && attention.data !== undefined ? " · stale" : ""}</Link>
      {!hidden && other > 0 && <IconButton label="Collapse other notices" icon={X} onClick={onHide} />}
    </section>
  );
}

export function Layout({ username, children }: { username: string; children: ReactNode }) {
  const nav = navFor(useRole().owner, useGroupAdministration(), useInstance().features);
  const [launching, setLaunching] = useState(false);
  const [launcherLoaded, setLauncherLoaded] = useState(false);
  const [preset, setPreset] = useState<NewSessionPreset | undefined>(undefined);
  const [dialogKey, setDialogKey] = useState(0); // bumped after a launch so the next one starts from a clean form
  const [palette, setPalette] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const { choice: theme, toggle } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const phone = useMedia("(max-width: 639px)");
  const statusBelowContent = phone && /^\/sessions\/[^/]+\/?$/.test(location.pathname);
  const [noticesHidden, setNoticesHidden] = useState(false);
  const inboxStatus = <InboxStatus hidden={noticesHidden} onHide={() => setNoticesHidden(true)} />;
  const queryClient = useQueryClient();
  const attention = useAttention();
  const crumbs = useCrumbs();
  const openItems = attention.data ?? [];
  // Signed out only once the server says so (code-quality review UI-1): on a failure the sign-in may still be valid, so
  // nothing is cleared and the person is told. A 401 means the sign-in had already ended, which is the outcome asked for.
  const signOut = useMutation({
    mutationFn: () => post("/api/auth/logout").catch((error) => { if (!(error instanceof ApiError && error.status === 401)) throw error; }),
    onSuccess: () => { queryClient.clear(); navigate("/login"); },
  });
  const logout = () => signOut.mutate();
  const newSession = useCallback((next?: NewSessionPreset) => {
    setLauncherLoaded(true);
    if (next) {  // a fresh form for each preset; a plain New session keeps the last draft (UX review NS-6)
      setPreset(next);
      setDialogKey((k) => k + 1);
    }
    setLaunching(true);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); return; }
      if (e.defaultPrevented) return;
      const typing = (e.target as HTMLElement)?.closest?.("input, textarea, select, [contenteditable], .xterm");
      if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey && e.key === "c" && !document.querySelector("[role=dialog]")) { e.preventDefault(); newSession(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newSession]);
  useEffect(() => setDrawer(false), [location.pathname]);
  const phoneTabs = nav.flatMap((g) => g.items).filter((n) => PHONE_TABS.includes(n.to));

  return (
    <UnsavedBriefGuard><div className="flex h-full overflow-hidden">
      <a href="#main" className="sr-only z-[70] rounded-md bg-accent px-3 py-2 text-accent-ink focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:inline-flex focus:min-h-11 focus:items-center">Skip to content</a>
      <aside className="hidden w-60 shrink-0 border-r border-line bg-panel md:block">
        <Sidebar username={username} onTheme={toggle} theme={theme} onLogout={logout} />
      </aside>
      {/* The phone drawer is a modal sheet: focus moves in and stays, Esc closes it, focus returns to Menu (UX review NAV-3). */}
      <Dialog sheet open={drawer} onClose={() => setDrawer(false)} title="Menu">
        <Sidebar username={username} onTheme={toggle} theme={theme} onLogout={logout} onNavigate={() => setDrawer(false)} />
      </Dialog>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* The installed iPhone app draws under a translucent status bar: pad by the safe area so the bar stays tappable. */}
        <header className="box-content flex h-14 shrink-0 items-center gap-3 border-b border-line bg-bg/80 px-3 pt-[env(safe-area-inset-top)] backdrop-blur md:px-6">
          <IconButton label="Menu" icon={Menu} aria-haspopup="dialog" aria-expanded={drawer} onClick={() => setDrawer(true)} className="md:hidden" />
          <nav aria-label="Breadcrumb" className="flex min-w-0 flex-1 items-center gap-1.5 text-[13px]">
            {crumbs.map((c, i) => (
              <span key={i} className="flex min-w-0 items-center gap-1.5">
                {i > 0 && <ChevronRight size={14} className="shrink-0 text-faint" aria-hidden="true" />}
                {c.to ? <Link to={c.to} className="truncate text-muted hover:text-fg pointer-coarse:inline-block pointer-coarse:min-w-11 pointer-coarse:py-3 pointer-coarse:leading-5">{c.label}</Link>
                  : <span className="truncate font-medium text-fg" aria-current="page">{c.label}</span>}
              </span>
            ))}
          </nav>
          <LiveIndicator />
          {/* The key caps sit beside the button's text, not in it, so its name is exactly what it shows (UX review NAV-7). */}
          <div className="relative hidden w-64 lg:block">
            <button onClick={() => setPalette(true)} aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"}
              className="flex h-8 w-full items-center gap-2 rounded-md border border-line bg-panel px-2.5 pr-16 text-[13px] text-faint transition-colors hover:border-line-strong hover:text-muted">
              <CornerDownRight size={14} aria-hidden="true" /> <span className="flex-1 text-left">Jump to…</span>
            </button>
            <span aria-hidden="true" className="pointer-events-none absolute right-2.5 top-1.5 flex gap-1"><Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd><Kbd>K</Kbd></span>
          </div>
          <IconButton label="Jump to…" icon={CornerDownRight} aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"} onClick={() => setPalette(true)} className="lg:hidden" />
          <Button variant="primary" onClick={() => newSession()} aria-label="New session">
            <Plus size={15} aria-hidden="true" /> <span className="hidden sm:inline">New session</span>
          </Button>
        </header>
        <ConnectionBanner />
        {!statusBelowContent && inboxStatus}
        <main id="main" tabIndex={0} aria-label="Content" className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain focus:outline-none">
          <NewSessionContext.Provider value={newSession}>
            <ErrorBoundary resetKey={location.pathname} reloadOnRetry>{children}</ErrorBoundary>
          </NewSessionContext.Provider>
        </main>
        {statusBelowContent && inboxStatus}
        <nav aria-label="Primary" className="grid shrink-0 grid-cols-4 border-t border-line bg-panel pb-[env(safe-area-inset-bottom)] md:hidden">
          {phoneTabs.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end ?? false} className={({ isActive }) => clsx("relative flex h-14 flex-col items-center justify-center gap-1 text-[11px]",
              isActive ? "text-fg" : "text-muted")}>
              <Icon size={19} aria-hidden="true" /> {label}
              {to === "/inbox" && openItems.length > 0 && <span aria-label={`${openItems.length} open`}
                className={clsx("tnum absolute right-[calc(50%-20px)] top-2 min-w-4 rounded-full px-1 text-[10px] font-semibold leading-4", inboxBadgeTone(openItems))}>{openItems.length}</span>}
            </NavLink>
          ))}
        </nav>
      </div>
      {launcherLoaded && <Suspense fallback={<span role="status" className="sr-only">Opening New session…</span>}><NewSessionDialog key={dialogKey} open={launching} preset={preset} onClose={() => setLaunching(false)}
        onLaunched={() => { setLaunching(false); setPreset(undefined); setDialogKey((k) => k + 1); }}
        onDiscard={() => { setPreset(undefined); setDialogKey((k) => k + 1); }} /></Suspense>}
      <Dialog open={signOut.isError} onClose={() => signOut.reset()} title="You're still signed in"
        footer={<>
          <Button variant="ghost" onClick={() => signOut.reset()}>Close</Button>
          <Button variant="primary" disabled={signOut.isPending} onClick={() => signOut.mutate()}>Try again</Button>
        </>}>
        <div className="space-y-3 text-[13px] text-muted">
          <p>Rynhult didn't confirm the sign-out, so this browser may still be signed in. Check the connection and try again; on a shared computer, don't leave until it works.</p>
          <ErrorNote error={signOut.error} />
        </div>
      </Dialog>
      <CommandPalette open={palette} onClose={() => setPalette(false)} onNewSession={() => newSession()} onToggleTheme={toggle} />
    </div></UnsavedBriefGuard>
  );
}
