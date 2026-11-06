import { useLocation } from 'react-router-dom';
import { lazy, useEffect, useState, type ReactNode } from 'react';
import { lazyImport } from '../lib/lazyImport';
import { SetupChoice, useSetupPreference } from '../lib/setupPreference';
const SetupLevelSetting = lazy(() => lazyImport(() => import('./SetupLevelSetting')).then(m => ({ default: m.SetupLevelSetting })));
import { ShellChoice, useShellPreference } from '../lib/uiShell';
const Layout = lazy(() => lazyImport(() => import('./Layout')).then(m => ({ default: m.Layout })));
const Shell = lazy(() => lazyImport(() => import('./shell/Shell')).then(m => ({ default: m.Shell })));
export interface Choice { person: string; enabled: boolean; personId: string | null }
/** App owns this latch, including while /board sits outside either frame. */
export default function LayoutChoice({ username, available, choice, choose, children }: {
  username: string; available: boolean; choice: Choice | null; choose: (choice: Choice) => void; children: ReactNode;
}) {
  const route = useLocation();
  // Cold Board documents do not need a frame. Once mounted, retain the new
  // frame during Board visits so working-view resources keep their lifetime.
  const [frameVisited, setFrameVisited] = useState(route.pathname !== '/board');
  useEffect(() => { if (route.pathname !== '/board') setFrameVisited(true); }, [route.pathname]);
  const pref = useShellPreference(username, available);
  const shell = choice?.person === username && choice.enabled;
  const setup = useSetupPreference(username, shell);
  useEffect(() => {
    if (choice?.person === username || available && pref.isPending) return;
    choose({ person: username, enabled: available && pref.data?.enabled === true, personId: pref.data?.person_id ?? null });
  }, [username, available, pref.isPending, pref.data, choice, choose]);
  if (choice?.person !== username) return <main id="main" tabIndex={-1} aria-label="Content"><p role="status">Loading layout…</p></main>;
  if (route.pathname === "/board" && (!choice.enabled || !frameVisited)) return <>{children}</>;
  const Frame = choice.enabled ? Shell : Layout;
  return <ShellChoice.Provider value={choice.enabled}><SetupChoice.Provider value={shell ? setup.data?.level ?? null : null}><Frame username={username} personId={choice.personId}>{shell && route.pathname === "/" && <div className="mx-auto w-full max-w-7xl p-4 empty:hidden"><SetupLevelSetting firstRun /></div>}{children}</Frame></SetupChoice.Provider></ShellChoice.Provider>;
}
