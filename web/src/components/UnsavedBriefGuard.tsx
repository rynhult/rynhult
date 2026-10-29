import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useBlocker } from 'react-router-dom';
import { useConfirm } from './confirm';

const DraftContext = createContext<() => () => void>(() => () => undefined);
const NavigationConsent = createContext<(path: string) => void>(() => undefined);

/** One router blocker covers both the project editor and the grant's compact editor. */
export function UnsavedBriefGuard({ children }: { children: ReactNode }) {
  const [editors, setEditors] = useState(0);
  const register = useCallback(() => {
    setEditors(n => n + 1);
    return () => setEditors(n => n - 1);
  }, []);
  const confirm = useConfirm();
  const asking = useRef(false);
  const allowedPath = useRef<string | undefined>(undefined);
  const allowNavigation = useCallback((path: string) => { allowedPath.current = path; }, []);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    const allowed = allowedPath.current === nextLocation.pathname;
    allowedPath.current = undefined;
    return !allowed && editors > 0
      && `${currentLocation.pathname}${currentLocation.search}${currentLocation.hash}`
        !== `${nextLocation.pathname}${nextLocation.search}${nextLocation.hash}`;
  });
  useEffect(() => {
    if (!editors) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [editors]);
  useEffect(() => {
    if (blocker.state !== 'blocked' || asking.current) return;
    asking.current = true;
    void confirm({ title: 'Leave with an unsaved project brief?',
      body: 'Your draft has not been saved. The team cannot read unsaved changes. This tab will try to keep the draft, but save it before granting a team.',
      confirm: 'Leave without saving' }).then(leave => {
      asking.current = false;
      if (leave) blocker.proceed(); else blocker.reset();
    });
  }, [blocker, confirm]);
  return <NavigationConsent.Provider value={allowNavigation}><DraftContext.Provider value={register}>{children}</DraftContext.Provider></NavigationConsent.Provider>;
}

export function useBriefNavigationConsent() { return useContext(NavigationConsent); }

export function useUnsavedBrief(dirty: boolean) {
  const register = useContext(DraftContext);
  useEffect(() => { if (dirty) return register(); }, [dirty, register]);
}
