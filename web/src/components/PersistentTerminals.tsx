import { createContext, lazy, Suspense, useContext, useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { AuthState } from '../lib/types';
import { ViewRegistry, terminalKey, type TerminalReference } from '../lib/viewRegistry';
import { TerminalHostLayer, type HostedTerminal } from './terminalHostLayer';
import type { TerminalProps } from './Terminal';
import { ErrorBoundary } from './ErrorBoundary';
const Presentation = lazy(() => import('./Terminal').then(module => ({default: module.TerminalPresentation})));

/** Latched at build/page load. No runtime preference or saved layout can enable it. */
export const persistentTerminalFlag = import.meta.env.VITE_P2_TERMINAL_HOST === '1';
type Record = { key: string; reference: TerminalReference; resource: HostedTerminal; props?: TerminalProps };
export class TerminalHosts {
  constructor(readonly person: string) {}
  readonly layer = new TerminalHostLayer(true, () => this.changed(), reference => {
    this.registry.dispose(terminalKey(reference), 'permission-revoked'); this.changed();
  });
  readonly records = new Map<string, Record>();
  readonly registry = new ViewRegistry<HTMLElement>({ create: (reference, policy) => {
    const resource = this.layer.create(reference, policy), key = terminalKey(reference);
    this.records.set(key, { key, reference, resource }); this.changed();
    return resource;
  } });
  private listeners = new Set<() => void>();
  private snapshot: Record[] = [];
  private stopped = false;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.snapshot;
  changed() {
    if (this.stopped) return;
    for (const [key, record] of this.records) if (record.resource.isDisposed) this.records.delete(key);
    this.snapshot = [...this.records.values()];
    for (const fn of this.listeners) fn();
  }
  describe(key: string, props: TerminalProps) {
    const record = this.records.get(key);
    if (record) { record.props = props; this.changed(); }
  }
  shutdown() {
    this.stopped = true; this.registry.shutdown(); this.layer.dispose(); this.records.clear();
    this.snapshot = []; for (const fn of this.listeners) fn(); this.listeners.clear();
  }
}
const Hosts = createContext<TerminalHosts | null>(null);
export const useTerminalHosts = () => useContext(Hosts);

export function PersistentTerminalProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const auth = useQuery({ queryKey: ['auth'], queryFn: () => api<AuthState>('/api/auth/state') });
  const person = auth.data?.authenticated && !auth.data.step_up_required ? auth.data.username : null;
  const [owned, setOwned] = useState<{person: string; hosts: TerminalHosts} | null>(null);
  const hosts = owned?.person === person ? owned.hosts : null;
  useEffect(() => {
    if (!person) { setOwned(null); return; }
    const owner = new TerminalHosts(person); setOwned({person, hosts: owner});
    // queryClient.clear() removes queries without refreshing every existing observer.
    // Sign-out must destroy resources immediately, even with retained observer data.
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (event.type === 'removed' && event.query.queryKey[0] === 'auth') {
        owner.shutdown(); setOwned(null);
      }
    });
    const sweep = window.setInterval(() => owner.registry.sweep(), 30_000);
    const hide = () => { clearInterval(sweep); unsubscribe(); owner.shutdown(); setOwned(null); };
    window.addEventListener('pagehide', hide, { once: true });
    return () => { unsubscribe(); clearInterval(sweep); window.removeEventListener('pagehide', hide); owner.shutdown(); };
  }, [person, client]);
  // Never expose a previous person's resources during a changed-auth render.
  return <Hosts.Provider value={person ? hosts : null}>{children}{person && hosts && <HostedViews hosts={hosts}/>}</Hosts.Provider>;
}
function HostedViews({ hosts }: { hosts: TerminalHosts }) {
  const records = useSyncExternalStore(hosts.subscribe, hosts.getSnapshot);
  const [error, setError] = useState('');
  const hidden = records.filter(record => !record.resource.isVisible);
  return <>{hidden.length > 0 && <aside aria-label="Background terminals" className="fixed bottom-2 right-2 z-30 rounded border border-line bg-panel p-2 text-xs text-muted">
    {hidden.length} background observer {hidden.length === 1 ? 'terminal' : 'terminals'}.{' '}
    <button type="button" className="text-accent underline" onClick={() => {
      try { for (const record of hidden) hosts.registry.dispose(record.key, 'release'); hosts.changed(); setError(''); }
      catch { setError('Finish composing before releasing this terminal.'); }
    }}>Release background terminals</button>{error && <p role="status">{error}</p>}
  </aside>}{records.map(record => record.props && createPortal(
    <ErrorBoundary resetKey={record.key} reloadOnRetry><Suspense fallback={null}><Presentation {...record.props} persistentResource={record.resource} active={record.resource.isVisible} onRelease={() => { hosts.registry.dispose(record.key, 'release'); hosts.changed(); }}/></Suspense></ErrorBoundary>, record.resource.host, record.key))}</>;
}

const noSubscribe = () => () => {};
const empty: Record[] = [];
const noSnapshot = () => empty;
type ReferenceReply = TerminalReference & { protocol: string };
/** A routed component owns only a slot and a view reference, never the host. */
export function PersistentTerminalSlot(props: TerminalProps) {
  const hosts = useTerminalHosts(), slot = useRef<HTMLDivElement>(null), id = useId();
  const [error, setError] = useState(''), [retry, setRetry] = useState(0);
  const [placement, setPlacement] = useState<{viewId: string; resourceKey: string} | null>(null);
  useSyncExternalStore(hosts?.subscribe ?? noSubscribe, hosts?.getSnapshot ?? noSnapshot);
  const propsRef = useRef(props); propsRef.current = props;
  useEffect(() => {
    if (!hosts || !slot.current) return;
    let cancelled = false, view: string | undefined, unregister: (() => void) | undefined;
    setError(''); setPlacement(null);
    const controller = new AbortController();
    void api<ReferenceReply>(`/api/sessions/${props.sessionId}/terminal-reference`, { cache: 'no-store', signal: controller.signal }).then(reply => {
      if (cancelled) return;
      if (reply.protocol !== 'run-observer-v1' || reply.sessionId !== props.sessionId || reply.person !== hosts.person) throw new Error('unsupported');
      const reference = reply;
      const acquired = hosts.registry.acquire(reference); view = acquired.viewId;
      unregister = hosts.registry.registerSlot(id, slot.current!);
      hosts.registry.place(view, id); hosts.describe(acquired.resourceKey, propsRef.current); setPlacement(acquired);
    }).catch(() => {
      if (!cancelled) setError('Terminal attachment unavailable. Recheck access or release an existing terminal; no session was started.');
    });
    return () => { cancelled = true; controller.abort(); if (view) hosts.registry.closeView(view); unregister?.(); };
  }, [hosts, props.sessionId, id, retry]);
  useEffect(() => {
    if (hosts && placement && hosts.registry.inspect(placement.resourceKey)?.canonicalView === placement.viewId)
      hosts.describe(placement.resourceKey, propsRef.current);
  }, [hosts, placement, props.compact, props.name, props.live, props.tone, props.stateLabel, props.onExpansionChange]);
  const current = hosts && placement ? hosts.registry.inspect(placement.resourceKey) : undefined;
  return <div ref={slot} className="h-full min-h-0" data-terminal-slot={id}>
    {!hosts && <p role="status" className="p-3 text-sm text-muted">Terminal host unavailable. Reload to recheck access.</p>}
    {hosts && placement && !current && <div role="status" className="p-3 text-sm text-muted">Terminal detached. Recheck access to attach as an observer. <button type="button" className="text-accent underline" onClick={() => setRetry(value => value + 1)}>Recheck</button></div>}
    {current && !current.waiting && placement && current.canonicalView !== placement.viewId && <button type="button" className="m-3 text-sm text-accent underline" onClick={() => {
      try { hosts!.registry.place(placement.viewId, id); hosts!.describe(placement.resourceKey, propsRef.current); }
      catch { setError('Terminal placement unavailable.'); }
    }}>Show shared terminal here</button>}
    {error && <div role="status" className="p-3 text-sm text-muted">{error} <button type="button" className="text-accent underline" onClick={() => setRetry(value => value + 1)}>Recheck</button></div>}
  </div>;
}
