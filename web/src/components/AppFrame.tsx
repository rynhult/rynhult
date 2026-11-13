import { startPushPresence } from "../lib/pushPresence";
import { useLocation } from 'react-router-dom';
import { lazy, useEffect, useState, type ReactNode } from 'react';
import { lazyImport } from '../lib/lazyImport';
import { SetupChoice, useSetupPreference } from '../lib/setupPreference';
const Shell = lazy(() => lazyImport(() => import('./shell/Shell')).then(m => ({ default: m.Shell })));

export default function AppFrame({ username, personId, presenceEnabled, children }: {
  username: string; personId: string | null; presenceEnabled: boolean; children: ReactNode;
}) {
  const route = useLocation();
  // Cold Board documents need no frame. Retain an already mounted frame so
  // working-view resources keep their lifetime during subsequent Board visits.
  const [frameVisited, setFrameVisited] = useState(route.pathname !== '/board');
  useEffect(() => {
    if (route.pathname !== '/board') setFrameVisited(true);
    return presenceEnabled ? startPushPresence(route.pathname) : undefined;
  }, [route.pathname, presenceEnabled]);
  const setup = useSetupPreference(username, true);
  if (route.pathname === '/board' && !frameVisited) return <>{children}</>;
  return <SetupChoice.Provider value={setup.data?.level ?? null}><Shell username={username} personId={personId}>{children}</Shell></SetupChoice.Provider>;
}
