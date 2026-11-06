import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { Project, SessionDetail } from "../../lib/types";
import { TerminalOwner } from "../terminalOwner";
import { ViewAccess } from "./ViewAccess";
import { DocumentRegistry } from "../../lib/documentRegistry";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from 'react';
import { Link, useBlocker, useLocation, useNavigate } from 'react-router-dom';
import { useSessions } from '../../lib/queries';
import { closeView, columnMinimum, effectiveColumns, emptyLayout, moveView, openView, referenceFor, resizeColumns, type WorkingLayout, type ViewReference } from '../../lib/workingLayout';
import { LayoutSync, prunePendingLayouts, type SyncView } from '../../lib/layoutSync';
import type { Layout, LayoutRecord, LayoutTransport } from '../../lib/layoutStore';
import { fileTarget, fromServer, keepViews, resolvePlaceholders, toServer } from '../../lib/workingSerial';
import { WorkingView, type WorkingViewState } from '../../lib/workingView';
import { Columns2, Maximize2, Minimize2, X } from 'lucide-react';
import { announce, Button, IconButton, Select } from '../ui';
import { useConfirm } from '../confirm';
import { ErrorBoundary } from '../ErrorBoundary';
import './WorkingArea.css';

type Work = Parameters<WorkingViewState['setWork']>[0];
const hrefOf = (route: { pathname: string; search: string; hash: string }) => route.pathname + route.search + route.hash;
const transport: LayoutTransport = (path, init) => api<LayoutRecord>(path, init);
/** The preference class is latched for this page load, so resizing a window never swaps its saved arrangement. */
function deviceClass() {
  const width = window.innerWidth;
  return { device: width < 768 ? 'phone' : window.matchMedia('(pointer: coarse)').matches ? 'tablet' : 'desktop', viewport: width >= 1200 ? 'wide' : width >= 768 ? 'medium' : 'narrow' };
}

/** Stable keyed siblings own page/editor DOM. Columns contain placements, never resource subtrees. */
export function WorkingArea({ personId, children }: { personId: string | null; children: ReactNode }) {
  const [terminals] = useState(() => new TerminalOwner());
  useSyncExternalStore(terminals.subscribe, terminals.snapshot);
  useEffect(() => { const timer = setInterval(() => terminals.sweep(), 30000); return () => { clearInterval(timer); terminals.shutdown(); }; }, [terminals]);
  const [documents] = useState(() => new DocumentRegistry());
  useEffect(() => () => documents.clear(), [documents]);
  const route = useLocation(); const navigate = useNavigate(); const confirm = useConfirm();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), refetchInterval: 30000 }); const sessions = useSessions();
  const reference = referenceFor(hrefOf(route));
  const sessionId = /^\/sessions\/([^/]+)/.exec(route.pathname)?.[1] ?? new URLSearchParams(route.search).get('session');
  const session = sessions.data?.find(item => item.id === sessionId);
  const projectId = /^\/projects\/(\d+)/.exec(route.pathname)?.[1] ?? session?.project_id;
  const scope = projectId ? `project:${projectId}` : sessionId ? `workspace:${sessionId}` : 'working';
  const [device] = useState(deviceClass);
  const syncs = useRef(new Map<string, LayoutSync>());
  const ready = useRef(new Set<string>());
  const person = useRef('');
  const fileRefs = useRef(new Map<string, string>());
  const persisting = useRef(new Map<string, number>());
  const [connected, setConnected] = useState(0);
  const [syncViews, setSyncViews] = useState<Record<string, SyncView>>({});
  useEffect(() => () => { for (const sync of syncs.current.values()) sync.close(); }, []);
  useEffect(() => {
    if (projects.isSuccess && person.current) prunePendingLayouts(person.current.split('\n')[0], person.current.split('\n')[1], new Set(projects.data.map(item => item.id)));
  }, [projects.data, projects.isSuccess, connected]);
  const [books, setBooks] = useState<Record<string, WorkingLayout>>({});
  const booksNow = useRef(books); booksNow.current = books;
  const [message, setMessage] = useState('');
  const [access, setAccess] = useState<Record<string, boolean | undefined>>({});
  const accessChanged = useCallback((key: string, available: boolean | undefined) => setAccess(old => old[key] === available ? old : { ...old, [key]: available }), []);
  const [retained, setRetained] = useState<Record<string, ViewReference>>({});
  const retainedNow = useRef(retained); retainedNow.current = retained;
  const work = useRef(new Map<string, Work>());
  const explicitViews = useRef(new Set<string>());
  if (reference) explicitViews.current.add(reference.key);
  const composing = useRef(new Set<string>()); const deferred = useRef<(() => void) | null>(null);
  const blocker = useBlocker(() => composing.current.size > 0);
  const [compositionEnded, setCompositionEnded] = useState(0);
  useEffect(() => { if (blocker.state === "blocked" && !composing.current.size) blocker.proceed(); }, [blocker, compositionEnded]);
  terminals.composition = (reference, active) => {
    const key = `session:${reference.sessionId}:terminal`;
    if (active) composing.current.add(key);
    else { composing.current.delete(key); requestAnimationFrame(() => { const pending = deferred.current; deferred.current = null; pending?.(); setCompositionEnded(value => value + 1); }); }
  };
  const area = useRef<HTMLDivElement>(null); const [width, setWidth] = useState(0);
  const [measurement, measure] = useState(0);
  const sizeObserver = useRef<ResizeObserver | null>(null);
  const layout = books[scope] ?? emptyLayout(); const current = layout.columns[layout.focused]?.active;
  const visible = !!reference; const single = effectiveColumns(layout, width) === 1;
  const slots = useRef(new Map<number, HTMLDivElement>());
  const slotRefs = useMemo(() => [0, 1, 2].map(index => (node: HTMLDivElement | null) => {
    const old = slots.current.get(index); if (old && old !== node) sizeObserver.current?.unobserve(old);
    if (node) { slots.current.set(index, node); sizeObserver.current?.observe(node); }
    else slots.current.delete(index);
  }), []);
  const [geometry, setGeometry] = useState<Record<string, CSSProperties>>({});
  useLayoutEffect(() => terminals.refreshGeometry(), [geometry, terminals]);
  /** Mints references here, so the saved layout never carries a path. Offline, an unreferenced file view waits for a later save. */
  const persist = useCallback(async (target: string, next: WorkingLayout | null) => {
    const sync = syncs.current.get(target); if (!sync) return;
    const turn = (persisting.current.get(target) ?? 0) + 1; persisting.current.set(target, turn);
    if (next === null) { sync.change(null); return; }
    const project = target.slice('project:'.length);
    for (const view of Object.values(next.views)) {
      const file = fileTarget(view);
      if (!file || fileRefs.current.has(view.key)) continue;
      try { fileRefs.current.set(view.key, (await api<{ file_ref: string }>(`/api/projects/${project}/layouts/file-refs`, { json: file })).file_ref); }
      catch { /* Counted as omitted below. */ }
    }
    if (persisting.current.get(target) !== turn) return;
    const saved = toServer(next, fileRefs.current);
    setMessage(saved.omitted ? 'Some views can\'t be saved yet. They stay open in this window.' : '');
    sync.change(saved.layout);
  }, []);
  const update = useCallback((next: WorkingLayout) => {
    setBooks(previous => ({ ...previous, [scope]: next }));
    setMessage(''); void persist(scope, next);
  }, [scope, persist]);
  /** Shows an arrangement. Terminals mount as read-only observers; nothing here sends, claims, starts or stops. */
  const show = useCallback((target: string, record: Pick<LayoutRecord, 'layout' | 'resources'>, layout?: Layout | null, keep: string[] = []) => {
    const restored = fromServer(record, layout === undefined ? record.layout : layout);
    // Throws at the view limit, so it runs before anything is changed.
    const next = restored.layout || keep.length ? keepViews(restored.layout, booksNow.current[target] ?? emptyLayout(), keep) : null;
    for (const [key, reference] of restored.refs) fileRefs.current.set(key, reference);
    if (!next) { setBooks(previous => { const value = { ...previous }; delete value[target]; return value; }); return null; }
    setRetained(old => ({ ...old, ...next.views }));
    setBooks(previous => ({ ...previous, [target]: next }));
    return next;
  }, []);
  const connect = useCallback((target: string) => {
    if (syncs.current.has(target)) return;
    const finish = () => { ready.current.add(target); setConnected(value => value + 1); };
    if (!target.startsWith('project:')) { setSyncViews(old => ({ ...old, [target]: { status: 'local', durable: false, note: 'Columns here last for this visit.' } })); finish(); return; }
    const sync = new LayoutSync(`/api/projects/${target.slice('project:'.length)}/layouts/${device.device}/${device.viewport}`, {
      transport, onChange: view => setSyncViews(old => ({ ...old, [target]: view })),
      onScope: identity => { person.current = `${identity.installation}\n${identity.person}`; prunePendingLayouts(identity.installation, identity.person); },
      onSaved: record => {
        const book = booksNow.current[target]; if (!book) return;
        // References are pruned once no saved layout uses them, so one held here can be gone. Ask again rather than save a dead one.
        const dead = Object.keys(book.views).filter(key => { const reference = fileRefs.current.get(key); return reference && record.resources[`f-${reference}`]?.available === false; });
        for (const key of dead) fileRefs.current.delete(key);
        if (dead.length) void persist(target, book);
        const resolved = resolvePlaceholders(book, record.resources); if (resolved.layout === book) return;
        for (const [key, reference] of resolved.refs) fileRefs.current.set(key, reference);
        setRetained(old => ({ ...old, ...resolved.layout.views })); setBooks(previous => ({ ...previous, [target]: resolved.layout }));
      },
    });
    syncs.current.set(target, sync);
    void sync.start().then(({ record, pending }) => { if (record) show(target, record, pending); finish(); }, () => { /* Closed while loading. */ });
  }, [device, show, persist]);
  const works = () => [...work.current].filter(([, value]) => value.dirty || value.uncertain || value.composing).map(([key]) => key);
  const useNewer = () => act(() => {
    const sync = syncs.current.get(scope); const record = sync?.newer(); if (!sync || !record) return;
    let next: WorkingLayout | null;
    try { next = show(scope, record, undefined, [...works(), ...terminals.retainedWriters()]); }
    catch { setMessage('The newer layout is full, so views with drafts or terminal control can\'t join it. Close a view here, or save your draft, then choose again.'); return; }
    sync.chooseNewer(); setMessage('');
    const active = next?.columns[next.focused]?.active; const selected = active ? next?.views[active] : undefined;
    if (selected?.href && !(reference && next?.views[reference.key])) navigate(selected.href, { replace: true });
    if (!next && reference) { const opened = openView(emptyLayout(), reference); setRetained(old => ({ ...old, ...opened.views })); setBooks(old => ({ ...old, [scope]: opened })); }
  });
  const act = (action: () => void) => {
    if (composing.current.size) { deferred.current = action; announce('Finish composing to apply the column change.'); }
    else action();
  };
  useEffect(() => {
    if (!reference) return;
    // Session project membership is resolved from the authorized inventory before choosing a storage namespace.
    if (sessionId && sessions.isPending) return;
    if (!ready.current.has(scope)) { connect(scope); return; }
    try {
      const base = booksNow.current[scope] ?? emptyLayout();
      const next = openView(base, reference);
      if (new Set([...Object.keys(retainedNow.current), ...Object.keys(next.views)]).size > 32) { setMessage('Open view limit reached. Close a view before opening another.'); return; }
      setRetained(old => ({ ...old, ...next.views }));
      setBooks(previous => ({ ...previous, [scope]: next }));
      // Revisiting a placed view is not an edit: loading or following a link to it never writes.
      if (!base.views[reference.key]) void persist(scope, next);
    } catch (error) { setMessage((error as Error).message); }
  }, [route.key, route.pathname, route.search, route.hash, scope, sessionId, sessions.isPending, connected, connect, persist]); // Address bar changes select references; geometry never navigates.
  useEffect(() => {
    const node = area.current; if (!node) return;
    const observer = new ResizeObserver(() => { setWidth(node.clientWidth); measure(value => value + 1); }); sizeObserver.current = observer; observer.observe(node); for (const slot of slots.current.values()) observer.observe(slot); setWidth(node.clientWidth);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (composing.current.size) return;
    const root = area.current?.getBoundingClientRect(); if (!root) return;
    const next: Record<string, CSSProperties> = {};
    if (visible) layout.columns.forEach((column, index) => {
      const key = layout.filled ?? column.active;
      if (!key || single && index !== layout.focused) return;
      const box = slots.current.get(index)?.getBoundingClientRect();
      if (box) next[key] = { left: box.left - root.left, top: box.top - root.top, width: box.width, height: box.height };
    });
    setGeometry(next);
  }, [books, scope, width, visible, single, layout.filled, layout.focused, compositionEnded, measurement]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (terminals.hasPendingInput() || [...work.current.values()].some(value => value.dirty || value.uncertain || value.composing)) event.preventDefault();
    };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  // A long tab strip scrolls, so keep the selected tab in view wherever the selection came from.
  useEffect(() => { area.current?.querySelector('.working-tabs [aria-selected=true]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }, [current, scope, visible, access, sessions.data, projects.data]);
  const focus = (key: string, navigation = true) => act(() => {
    const next = openView(layout, layout.views[key]); update(next);
    if (navigation && next.views[key].href && hrefOf(route) !== next.views[key].href) navigate(next.views[key].href);
    requestAnimationFrame(() => area.current?.querySelector<HTMLButtonElement>(`[data-view-tab="${CSS.escape(key)}"]`)?.focus());
  });
  const move = (key: string, destination: number, before?: string) => act(() => {
    const fromTab = document.activeElement?.hasAttribute('data-view-tab');
    const next = moveView(layout, key, destination, before); update(next);
    announce(`View moved to column ${destination + 1}.`);
    if (fromTab) requestAnimationFrame(() => area.current?.querySelector<HTMLButtonElement>(`[data-view-tab="${CSS.escape(key)}"]`)?.focus());
  });
  const canMove = (key: string, destination: number) => {
    if (destination < layout.columns.length) return true;
    const proposed = moveView(layout, key, destination);
    return effectiveColumns(proposed, width) === proposed.columns.length;
  };
  async function close(key: string) {
    const state = work.current.get(key);
    if (composing.current.has(key) || state?.composing || state?.uncertain) { setMessage('Finish composing or wait for the pending save before closing this view.'); return; }
    if (state?.dirty && !await confirm({ title: 'Discard unsaved edits?', body: 'Closing this file discards its unsaved draft.', confirm: 'Discard draft', danger: true })) return;
    const next = closeView(layout, key); update(next);
    if (layout.views[key]?.kind !== 'terminal' || !terminals.resource(key)) setRetained(old => { const value = { ...old }; delete value[key]; return value; });
    work.current.delete(key); documents.close(key); fileRefs.current.delete(key);
    const selected = next.columns[next.focused].active;
    if (selected && next.views[selected].href) navigate(next.views[selected].href, { replace: true });
    announce('View closed. Running sessions continue.');
  }
  function label(view: ViewReference) {
    if (view.kind === 'unavailable') return 'View unavailable';
    const url = new URL(view.href, location.origin);
    const id = /^\/sessions\/([^/]+)/.exec(url.pathname)?.[1];
    const item = sessions.data?.find(value => value.id === id);
    const project = projects.data?.find(value => String(value.id) === url.pathname.split('/')[2]);
    const part = (fallback: string) => { const tab = url.searchParams.get('tab') ?? fallback; return tab[0].toUpperCase() + tab.slice(1); };
    // Cached names are shown only after the restored reference passes its access check.
    if (access[view.key] === undefined) return 'Saved view';
    if (!access[view.key]) return 'View unavailable';
    const file = url.searchParams.get('file'); if (file) return `${file} · ${url.pathname.split('/')[2]}`;
    if (id) return item ? `${item.name} · ${part('Terminal')}` : 'Unavailable session';
    return project ? `${project.name} · ${part('Overview')}` : 'Unavailable view';
  }
  const normalized = resizeColumns(layout, -1, 0, width);
  const syncView = syncViews[scope];
  const sync = syncView && (syncView.status === 'conflict' || syncView.status === 'offline' || syncView.note) ? syncView : null;
  const shownColumns = layout.columns.map((column, index) => ({ column, index })).filter(({ index }) => !single || index === layout.focused);
  const drag = useRef<{ divider: number; start: number; layout: WorkingLayout } | null>(null);
  const toolbar = <div className="working-toolbar" aria-label="Working area controls">
    {single && layout.columns.length > 1 && !layout.filled && <span className="text-xs text-muted">One column fits. All views remain available.</span>}
    <IconButton label={layout.filled ? 'Restore columns' : 'Fill working panel'} icon={layout.filled ? Minimize2 : Maximize2} disabled={!current} onClick={() => act(() => update({ ...layout, filled: layout.filled ? null : current }))} />
    <Select aria-label="Move view to column" title="Move view to column" disabled={!current} value={layout.focused} onChange={event => current && move(current, Number(event.target.value))}>{[0, 1, 2].map(index => <option key={index} value={index} disabled={!!current && !canMove(current, index)}>Column {index + 1}</option>)}</Select>
    {layout.columns.length > 1 && <label className="text-xs">Focus <Select aria-label="Focus column" value={layout.focused} onChange={event => { const index = Number(event.target.value); const key = layout.columns[index].active; if (key) focus(key); else update({ ...layout, focused: index, filled: null }); }}>{layout.columns.map((_, index) => <option key={index} value={index}>Column {index + 1}</option>)}</Select></label>}
    {layout.columns.length > 1 && !layout.columns[layout.focused].tabs.length && <Button size="sm" onClick={() => { const columns = layout.columns.filter((_, index) => index !== layout.focused); update({ ...layout, columns: columns.map(column => ({ ...column, width: 1 / columns.length })), focused: 0 }); }}>Remove empty column</Button>}
    <IconButton label="Reset columns" icon={Columns2} onClick={() => act(() => { setBooks(previous => ({ ...previous, [scope]: { ...layout, columns: [{ tabs: layout.columns.flatMap(column => column.tabs), active: current ?? Object.keys(layout.views)[0] ?? null, width: 1 }], focused: 0, filled: null } })); setMessage(''); fileRefs.current.clear(); void persist(scope, null); })} />
    <IconButton label="Close view" icon={X} disabled={!current} onClick={() => current && void close(current)} />
  </div>;
  return <div ref={area} className="working-area" data-working-columns={visible ? shownColumns.length : 0} data-working-panes={visible && shownColumns.length > 1 && !layout.filled || undefined}
    onCompositionStartCapture={event => { const key = (event.target as HTMLElement).closest<HTMLElement>('[data-working-view]')?.dataset.workingView; if (key) composing.current.add(key); }}
    onCompositionEndCapture={event => { const key = (event.target as HTMLElement).closest<HTMLElement>('[data-working-view]')?.dataset.workingView; if (key) composing.current.delete(key); requestAnimationFrame(() => { if (!composing.current.size) { const pending = deferred.current; deferred.current = null; pending?.(); setCompositionEnded(count => count + 1); } }); }}>
    {visible && <>
      {blocker.state === "blocked" && <p className="working-message">Finish composing to change views.</p>}
      {sync && <p className="working-message" role="status" data-layout-sync={sync.status}>{sync.status === 'conflict'
        ? <>This layout changed in another window. <Button size="sm" onClick={() => syncs.current.get(scope)?.chooseThisWindow()}>Use this window's layout</Button> <Button size="sm" onClick={useNewer}>Load the newer one</Button></>
        : sync.status === 'offline' ? (sync.durable ? 'Offline. Layout changes are kept on this device and are sent when the server returns.' : 'Offline. Layout changes last only while this window stays open.')
        : sync.note}</p>}
      <div className="working-columns">
        {shownColumns.map(({ column, index }, position) => <div className="working-column-pair" key={index} style={{ flex: single ? '1' : `${normalized.columns[index].width} 1 0` }}>
          {position > 0 && <div role="separator" aria-label={`Resize columns ${index} and ${index + 1}`} aria-orientation="vertical" aria-valuemin={columnMinimum(layout, layout.columns[index - 1])} aria-valuemax={Math.round(width - columnMinimum(layout, column))} aria-valuenow={Math.round(normalized.columns[index - 1].width * (width - (layout.columns.length - 1) * 8))} tabIndex={0} className="working-divider"
            onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); act(() => update(resizeColumns(layout, index - 1, event.key === 'Home' ? -width : event.key === 'End' ? width : (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 64 : 16), width))); } }}
            onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); drag.current = { divider: index - 1, start: event.clientX, layout }; }}
            onPointerMove={event => { const state = drag.current; if (state) act(() => update(resizeColumns(state.layout, state.divider, event.clientX - state.start, width))); }}
            onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} />}
          <section className="working-column" aria-label={`Column ${index + 1}`} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); const key = event.dataTransfer.getData('text/rynhult-view'); if (layout.views[key]) move(key, index); }}>
            <div className="working-tab-row"><div role="tablist" aria-label={`Column ${index + 1} views`} className="working-tabs">
              {(single ? layout.columns.flatMap(c => c.tabs) : column.tabs).map(key => <button key={key} type="button" role="tab" data-view-tab={key} aria-selected={single ? key === current : key === column.active} tabIndex={(single ? key === current : key === column.active) ? 0 : -1} draggable
                onDragStart={event => event.dataTransfer.setData('text/rynhult-view', key)}
                onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); const from = event.dataTransfer.getData('text/rynhult-view'); if (layout.views[from]) move(from, index, key); }}
                onClick={() => focus(key)} onKeyDown={event => {
                  if (event.nativeEvent.isComposing || event.keyCode === 229) return;
                  if (event.ctrlKey && event.shiftKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); const destination = index + (event.key === 'ArrowLeft' ? -1 : 1); if (destination >= 0 && destination < 3 && canMove(key, destination)) move(key, destination); return; }
                  const keys = single ? layout.columns.flatMap(c => c.tabs) : column.tabs; const at = keys.indexOf(key);
                  const next = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : event.key === 'ArrowLeft' ? (at - 1 + keys.length) % keys.length : event.key === 'ArrowRight' ? (at + 1) % keys.length : null;
                  if (next !== null) { event.preventDefault(); focus(keys[next]); }
                }}>{label(layout.views[key])}</button>)}
            </div>{position === shownColumns.length - 1 && <>{message && <span className="working-notice" role="status" title={message}>{message.startsWith("Some views") ? "Some views stay in this window" : message}</span>}{toolbar}</>}</div>
            <div className="working-slot" ref={slotRefs[index]}>
              {!column.active && <div className="working-empty"><p>Open a session or file from Browse.</p><Link to="/projects">Browse projects</Link><Link to="/sessions">Browse sessions</Link></div>}
            </div>
          </section>
        </div>)}
      </div>
    </>}
    {!visible && <div className="working-page">{children}</div>}
    {[...new Set(Object.values(retained).map(view => /^\/sessions\/([^/?]+)/.exec(view.href)?.[1]).filter((id): id is string => !!id))].map(id => <SessionLifetime key={id} id={id} />)}
    {Object.entries(retained).map(([key, view]) => <RetainedView key={key} allowArchived={explicitViews.current.has(key)} intent={reference?.key === key ? route.key : undefined} view={view} style={geometry[key]} focused={current === key} onAccess={accessChanged} openBeside={href => {
      const view = referenceFor(href); if (!view) return;
      act(() => {
        if (!retainedNow.current[view.key] && Object.keys(retainedNow.current).length >= 32) { setMessage('Open view limit reached. Close a view before opening another.'); return; }
        let next = openView(layout, view);
        if (!layout.views[view.key]) {
          const empty = layout.columns.findIndex(column => !column.tabs.length);
          const destination = empty >= 0 ? empty : Math.min(2, layout.columns.length);
          const proposed = moveView(next, view.key, destination);
          if (effectiveColumns(proposed, width) > 1) next = proposed;
        }
        setRetained(old => ({ ...old, [view.key]: view })); update(next); navigate(view.href);
      });
    }} documents={documents} terminals={terminals} person={personId ?? "service"} focusExisting={existing => { const entry = Object.values(books).find(book => book.views[existing]); if (entry) navigate(entry.views[existing].href); }} setWork={state => work.current.set(key, state)}>{children}</RetainedView>)}
  </div>;
}

function RetainedView({ allowArchived, intent, view, style, focused, documents, terminals, person, focusExisting, openBeside, onAccess, setWork, children }: { allowArchived: boolean; intent?: string; view: ViewReference; style?: CSSProperties; focused: boolean; onAccess: (key: string, available: boolean | undefined) => void; documents: DocumentRegistry; terminals: TerminalOwner; person: string; openBeside: (href: string) => void; focusExisting: (key: string) => void; setWork: WorkingViewState['setWork']; children: ReactNode }) {
  // Non-resource pages may be reconstructed. Terminal and Files renderers stay mounted while hidden.
  const visible = !!style;
  const [opened, setOpened] = useState(visible);
  useEffect(() => { if (visible) setOpened(true); }, [visible]);
  if (view.kind === 'unavailable') return <div className="working-view" data-working-view={view.key} style={{ ...style, visibility: visible ? 'visible' : 'hidden' }} inert={!visible} aria-hidden={!visible}>
    <div className="working-empty"><p>View unavailable. Access may have ended or the resource may have been removed.</p></div>
  </div>;
  const accessChanged = useCallback((available: boolean | undefined) => onAccess(view.key, available), [onAccess, view.key]);
  // Check saved references even while hidden; defer page/editor/terminal mounting until opened.
  const render = visible || opened && view.kind !== 'page';
  const value = useMemo(() => ({ key: view.key, href: view.href, visible, focused, explicit: !!intent, intent, documents, terminals, person, focusExisting, openBeside, setWork, setAvailable: accessChanged }), [view.key, view.href, visible, focused, allowArchived, intent, documents, terminals, person, focusExisting, openBeside, setWork, accessChanged]);
  return <div className="working-view" data-working-view={view.key} style={{ ...style, visibility: visible ? 'visible' : 'hidden' }} inert={!visible} aria-hidden={!visible}>
    <WorkingView.Provider value={value}><ErrorBoundary resetKey={view.key}><ViewAccess allowArchived={allowArchived} onAccess={accessChanged}>{render && children}</ViewAccess></ErrorBoundary></WorkingView.Provider>
  </div>;
}

/** One low-cost authorization/status watcher per retained session, independent of placement count. */
function SessionLifetime({ id }: { id: string }) {
  useQuery({ queryKey: ['sessions', id], queryFn: () => api<SessionDetail>(`/api/sessions/${id}`), refetchInterval: 30000, retry: false });
  return null;
}
