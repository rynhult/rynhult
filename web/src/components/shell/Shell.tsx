import { clearWorkingLayouts } from '../../lib/layoutPrivacy';
import { WorkingArea } from './WorkingArea';
import '../../shell-palette.css';
import './Shell.css';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type CSSProperties } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { LogOut, Menu, Moon, PanelLeftClose, PanelLeftOpen, Plus, User } from 'lucide-react';
import { ApiError, post } from '../../lib/api';
import { lazyImport } from '../../lib/lazyImport';
import { useAttention, useGroupAdministration, useHealth, useInstance, useProjects, useRole } from '../../lib/queries';
import { desktopBuild } from '../../lib/desktop';
import { THEME_LABEL } from '../../lib/themeChoice';
import { NewSessionContext, type NewSessionPreset } from '../newSession';
import { UnsavedBriefGuard } from '../UnsavedBriefGuard';
import { CurrentSecurityShield } from './SecurityShield';
import { ConnectionBanner, LiveIndicator, useTheme } from '../Layout';
import { CommandPalette } from '../CommandPalette';
import { ErrorBoundary } from '../ErrorBoundary';
import { announce, Button, Dialog, Dot, ErrorNote, IconButton, Popover, useMedia } from '../ui';
import { useUpdates } from '../Updates';
import { pathVisible, modeVisible } from '../../lib/setupLevels';
import { useSetupLevel } from '../../lib/setupPreference';
import { navFor } from '../nav';
import { modeForPath, railFor, toolsFor, type Mode } from './navigation';
import { localKey, projectIds } from './presentation';
import { Sidebar } from './Sidebar';
import { OptionalHeader } from './OptionalHeader';
const NewSessionDialog = lazy(() => lazyImport(() => import('../NewSessionDialog').then(m => ({ default: m.NewSessionDialog }))));

function useLocal<T>(personId: string | null, field: string, fallback: T, parse: (value: unknown) => T) {
  const key = localKey(location.origin, personId, field);
  const [value, setValue] = useState<T>(() => { try { return key ? parse(JSON.parse(localStorage.getItem(key) ?? 'null')) : fallback; } catch { return fallback; } });
  const save = (next: T) => { setValue(next); if (key) try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* Presentation remains usable without storage. */ } };
  return [value, save] as const;
}
const modes: Mode[] = ['overview', 'inbox', 'sessions', 'projects', 'workspaces', 'hosts', 'grid', 'tools', 'settings'];
export function Shell({ username, personId, children }: { username: string; personId: string | null; children: ReactNode }) {
  // Warm Status board trips keep this frame mounted but sit outside its palette.
  const outsideFrame = useLocation().pathname === '/board';
  useLayoutEffect(() => {
    if (outsideFrame) return;
    document.documentElement.dataset.shellPalette = 'mockup';
    return () => { delete document.documentElement.dataset.shellPalette; };
  }, [outsideFrame]);
  const instance = useInstance(); const role = useRole(); const { owner } = role; const groupAdmin = useGroupAdministration(); const health = useHealth();
  const level = useSetupLevel();
  const nav = railFor(owner, groupAdmin, instance.features, mode => modeVisible(level, mode)); const updates = useUpdates(owner);
  const [savedMode, setMode] = useLocal<Mode>(personId, 'mode', 'projects', x => modes.includes(x as Mode) ? x as Mode : 'projects');
  const route = useLocation();
  const primaryMode = modeForPath(route.pathname, nav, toolsFor(owner, groupAdmin, instance.features));
  const mode = primaryMode ?? (nav.some(n => n.id === savedMode) ? savedMode : nav[0]?.id ?? 'projects');
  const [width, setWidth] = useLocal(personId, 'width', 256, x => typeof x === 'number' && Number.isFinite(x) ? Math.max(208, Math.min(400, x)) : 256);
  const [collapsed, setCollapsed] = useLocal(personId, 'collapsed', false, x => x === true);
  const resizeStart = useRef<{ x: number; width: number } | null>(null);
  const resize = (next: number) => setWidth(Math.max(208, Math.min(400, Math.round(next))));
  const [pins, setPins] = useLocal(personId, 'pins', [] as number[], projectIds);
  const [recent, setRecent] = useLocal(personId, 'recent', [] as number[], projectIds);
  const [palette, setPalette] = useState(false); const [drawer, setDrawer] = useState(false);
  const [launching, setLaunching] = useState(false); const [loaded, setLoaded] = useState(false); const [preset, setPreset] = useState<NewSessionPreset>(); const [dialogKey, setDialogKey] = useState(0);
  const attention = useAttention();
  useEffect(() => { if (attention.isError) announce("Inbox unavailable. Notice counts may be out of date."); }, [attention.isError]);
  const { choice: theme, toggle } = useTheme(); const navigate = useNavigate(); const cache = useQueryClient();
  const contextHidden = mode === 'grid' || mode === 'overview';
  const phone = useMedia('(max-width: 767px)'); const projects = useProjects();
  const project = projects.data?.find(p => route.pathname.startsWith(`/projects/${p.id}/`) || route.pathname === `/projects/${p.id}`);
  const newSession = useCallback((next?: NewSessionPreset) => {
    setLoaded(true); if (next) { setPreset(next); setDialogKey(k => k + 1); }
    const search = new URLSearchParams(route.search); if (!search.has('new-session')) {
      search.set('new-session', '1'); navigate({pathname: route.pathname, search: search.toString(), hash: route.hash});
    } else setLaunching(true);
  }, [navigate, route.pathname, route.search, route.hash]);
  const closeLaunch = () => {
    const search = new URLSearchParams(route.search); search.delete('new-session');
    navigate({pathname: route.pathname, search: search.toString(), hash: route.hash}, {replace: true});
  };
  const wasLaunching = useRef(false);
  useEffect(() => {
    const open = new URLSearchParams(route.search).get('new-session') === '1';
    setLaunching(open); if (open) setLoaded(true);
  }, [route.search]);
  useEffect(() => { if (wasLaunching.current && !launching) document.getElementById('shell-new-session')?.focus(); wasLaunching.current=launching; }, [launching]);
  // A retained launcher draft wins over current navigation, as in Layout. Only an explicit project action resets it.
  const routeProjectId = /^\/projects\/(\d+)(?:\/|$)/.exec(route.pathname)?.[1];
  const launch = () => newSession(!loaded && routeProjectId ? { mode: 'project', project_id: Number(routeProjectId) } : undefined);
  const signOut = useMutation({ mutationFn: () => post('/api/auth/logout').catch(error => { if (!(error instanceof ApiError && error.status === 401)) throw error; }), onSuccess: () => { clearWorkingLayouts(); cache.clear(); navigate('/login'); } });
  useEffect(() => setDrawer(false), [route.pathname, route.search, route.hash]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette(p => !p); return; }
      if (e.defaultPrevented) return;
      if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === 'c' && !(e.target as HTMLElement)?.closest('input, textarea, select, [contenteditable], .xterm') && !document.querySelector('[role=dialog]')) { e.preventDefault(); newSession(); }
      if (e.key === 'F6' && !document.querySelector('[role=dialog]')) {
        const ids = phone ? ['shell-header', 'shell-phone-rail', 'main'] : ['shell-header', 'shell-rail', ...(collapsed || contextHidden ? [] : ['shell-sidebar']), 'main'];
        const current = ids.findIndex(id => document.getElementById(id)?.contains(document.activeElement));
        const next = (current + (e.shiftKey ? -1 : 1) + ids.length) % ids.length;
        e.preventDefault(); document.getElementById(ids[next])?.focus();
      }
    };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, [newSession, phone, collapsed, contextHidden]);
  // Retain the last shell target when responsive CSS blurs it to the body.
  const lastFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    // CSS can blur a hidden rail before the media-query effect runs.
    const active = document.activeElement === document.body ? lastFocus.current : document.activeElement as HTMLElement | null;
    const focusedMode = active?.closest<HTMLElement>('[data-shell-mode]')?.dataset.shellMode;
    if (phone && (document.getElementById('shell-rail')?.contains(active) || document.getElementById('shell-sidebar')?.contains(active))) {
      const controls = document.querySelectorAll<HTMLButtonElement>('#shell-phone-rail button');
      ([...controls].find(button => button.dataset.shellMode === focusedMode) ?? controls[controls.length - 1])?.focus();
    } else if (!phone) {
      const fromPhone = document.getElementById('shell-phone-rail')?.contains(active) || active?.closest('[data-shell-browse]');
      setDrawer(false);
      if (fromPhone) {
        const controls = document.querySelectorAll<HTMLButtonElement>('#shell-rail button');
        ([...controls].find(button => button.dataset.shellMode === (focusedMode ?? mode)) ?? controls[0])?.focus();
      }
    }
  }, [phone]);
  useEffect(() => { if (primaryMode && primaryMode !== savedMode) setMode(primaryMode); }, [primaryMode, savedMode]);
  const select = (next: Mode) => {
    const target = nav.find(item => item.id === next);
    if (!target) return;
    setMode(next); setCollapsed(false); setDrawer(false);
    navigate(target.to, next === 'inbox' ? { state: { inboxReturn: route.pathname === '/inbox' ? route.state?.inboxReturn : route.pathname + route.search + route.hash } } : undefined);
  };
  const inboxCount = attention.data?.length ?? 0;
  const inboxCountLabel = `${inboxCount > 99 ? '99+ shown, ' : ''}${inboxCount} open`;
  const inboxLabel = attention.isError
    ? `Inbox${attention.data ? `, ${inboxCountLabel}, count may be out of date` : ', unavailable'}`
    : inboxCount ? `Inbox, ${inboxCountLabel}` : 'Inbox';
  const rail = (items: typeof nav) => items.map(({ id, label, icon: Icon }) => <button key={id} type="button" className="shell-rail-button" data-shell-mode={id} aria-label={id === 'inbox' ? inboxLabel : undefined} title={id === 'inbox' ? inboxLabel : label} aria-pressed={mode === id} onClick={() => select(id)}><Icon size={18} aria-hidden="true" /><span>{label}</span>{id === 'inbox' && inboxCount > 0 && <> <span className="shell-inbox-count" aria-hidden="true">{inboxCount > 99 ? '99+' : inboxCount}</span></>}</button>);
  const railKeys = (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.defaultPrevented || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229 || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    const buttons = [...e.currentTarget.querySelectorAll<HTMLButtonElement>('button')]; const index = buttons.indexOf(e.target as HTMLButtonElement);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1 : ['ArrowDown', 'ArrowRight'].includes(e.key) ? (index + 1) % buttons.length : ['ArrowUp', 'ArrowLeft'].includes(e.key) ? (index - 1 + buttons.length) % buttons.length : null;
    if (next !== null) { e.preventDefault(); buttons[next]?.focus(); }
  };
  const sidebar = <Sidebar key={JSON.stringify([role.username, role.role, role.account])} mode={mode} pins={pins} recent={recent} pin={id => setPins(pins.includes(id) ? pins.filter(p => p !== id) : [...pins, id])} visit={id => setRecent([id, ...recent.filter(p => p !== id)].slice(0, 12))} close={() => setDrawer(false)} />;
  const tuckedAway = navFor(owner, groupAdmin, instance.features).flatMap(g => g.items).some(n => n.to !== '/' && (route.pathname === n.to || route.pathname.startsWith(`${n.to}/`)) && !pathVisible(level, n.to));
  return <UnsavedBriefGuard><NewSessionContext.Provider value={newSession}><div className="shell" data-board={route.pathname === "/board" || undefined} onFocusCapture={event => { lastFocus.current = event.target as HTMLElement; }} style={{ '--shell-sidebar-width': `${width}px` } as CSSProperties}>
    <a href="#main" className="shell-skip">Skip to content</a>
    <header id="shell-header" tabIndex={-1} className="shell-header" aria-label="Workspace header">
      <div className="shell-header-line"><Link to="/" className="shell-brand"><span className="shell-brand-mark"><img src="/rynhult-mark-light.svg" alt="" className="dark:hidden" /><img src="/rynhult-mark-dark.svg" alt="" className="hidden dark:block" /></span><strong>Rynhult</strong></Link>
        <span className="shell-host" title={instance.host}><Dot tone={health.isError || health.data?.broker === false ? 'bad' : health.data?.broker ? 'ok' : 'neutral'} />{instance.host}</span>
        <OptionalHeader username={username} owner={owner} goTo={() => setPalette(true)} />
      <div className="shell-critical"><CurrentSecurityShield /><LiveIndicator />{owner && updates.data?.available && <Link to="/settings#updates" className="text-wait underline">Update available</Link>}</div>
        <Button id="shell-new-session" variant="primary" className="shell-launch" onClick={launch} title={loaded ? 'Reopen your session draft' : project ? `New session in ${project.name}` : 'New session'}><Plus size={16} aria-hidden="true" /><span>{project && !loaded ? `New session · ${project.name}` : 'New session'}</span></Button>
        <Popover label={`Account: ${username}`} trigger={<User size={17} aria-hidden="true" />}>{close => <div className="space-y-2"><p>{username}</p><Button onClick={toggle}><Moon size={14} aria-hidden="true" />Theme: {THEME_LABEL[theme]}</Button>{!desktopBuild && <Button onClick={() => { close(); signOut.mutate(); }}><LogOut size={14} aria-hidden="true" />Sign out</Button>}</div>}</Popover>
      </div>

      <ConnectionBanner />
    </header>
    <div className="shell-body">
      <nav id="shell-rail" tabIndex={-1} className="shell-rail" aria-label="Rail" onKeyDown={railKeys}>{rail(nav)}</nav>
      <div hidden={contextHidden} className={`shell-sidebar-frame${collapsed ? ' is-collapsed' : ''}`}>
        <IconButton className="shell-collapse" label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} icon={collapsed ? PanelLeftOpen : PanelLeftClose} onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-controls="shell-sidebar" />
        <aside id="shell-sidebar" tabIndex={-1} hidden={collapsed || contextHidden} className="shell-sidebar" aria-label="Context sidebar">{!phone && !collapsed && sidebar}</aside>
        {!collapsed && <div className="shell-resize" role="separator" tabIndex={0} aria-label="Sidebar width" aria-orientation="vertical" aria-valuemin={208} aria-valuemax={400} aria-valuenow={width} aria-controls="shell-sidebar" title="Drag or use arrow keys to resize. Double-click to reset."
          onKeyDown={e => {
            if (e.altKey || e.ctrlKey || e.metaKey || e.nativeEvent.isComposing) return;
            const next = e.key === 'Home' ? 208 : e.key === 'End' ? 400 : e.key === 'ArrowLeft' ? width - 8 : e.key === 'ArrowRight' ? width + 8 : null;
            if (next !== null) { e.preventDefault(); resize(next); }
          }} onDoubleClick={() => resize(256)}
          onPointerDown={e => { if (e.button !== 0) return; e.preventDefault(); e.currentTarget.focus(); e.currentTarget.setPointerCapture(e.pointerId); resizeStart.current = { x: e.clientX, width }; }}
          onPointerMove={e => { if (resizeStart.current) resize(resizeStart.current.width + e.clientX - resizeStart.current.x); }}
          onPointerUp={e => { resizeStart.current = null; if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }}
          onLostPointerCapture={() => { resizeStart.current = null; }} onPointerCancel={() => { resizeStart.current = null; }} />}
      </div>
      <main id="main" tabIndex={0} aria-label="Content" className="shell-main"><div hidden={launching} className="h-full min-h-0"><ErrorBoundary resetKey={personId ?? username} reloadOnRetry><WorkingArea key={personId ?? username} personId={personId}>{tuckedAway && <p className="px-4 pt-3 text-sm text-muted">Not usually shown in your layout. <Link to="/settings#setup-level" className="underline">Change what I see</Link></p>}{children}</WorkingArea></ErrorBoundary></div>{loaded && <Suspense fallback={null}><NewSessionDialog key={dialogKey} open={launching} preset={preset} presentation="workspace" onClose={closeLaunch} onLaunched={() => { setLaunching(false); setPreset(undefined); setDialogKey(k => k + 1); }} onDiscard={() => { setPreset(undefined); setDialogKey(k => k + 1); }} /></Suspense>}</main>
    </div>
    <nav id="shell-phone-rail" tabIndex={-1} aria-label="Phone rail" className="shell-phone-rail" onKeyDown={railKeys}>{rail(nav.filter(n => ['overview', 'inbox', 'sessions', 'projects'].includes(n.id)))}<button className="shell-rail-button" onClick={() => setDrawer(true)} aria-haspopup="dialog" aria-expanded={drawer}><Menu size={18} aria-hidden="true" />More</button></nav>
    <Dialog sheet open={drawer} onClose={() => setDrawer(false)} title="Browse"><div className="shell-sheet-body" data-shell-browse><nav aria-label="Rail modes" className="shell-sheet-modes" onKeyDown={railKeys}>{rail(nav)}</nav>{phone && sidebar}</div></Dialog>
    <Dialog open={signOut.isError} onClose={() => signOut.reset()} title="You're still signed in"><ErrorNote error={signOut.error} /><Button onClick={() => signOut.mutate()}>Try again</Button></Dialog>
    <CommandPalette open={palette} onClose={() => setPalette(false)} onNewSession={() => newSession()} onToggleTheme={toggle} />
  </div></NewSessionContext.Provider></UnsavedBriefGuard>;
}
