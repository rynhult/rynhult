import type { Layout, LayoutFile, LayoutRecord, LayoutView } from './layoutStore.ts';
import { emptyLayout, openView, referenceFor, type ViewReference, type WorkingLayout } from './workingLayout.ts';

/** Between the live layout, whose hrefs and keys may name files, and the saved one, which never does. */
const ID = /^[A-Za-z0-9_-]{1,64}$/;
const TAB = /^[a-z][a-z-]{0,31}$/;
const placeholder = (saved: LayoutView): ViewReference => ({ key: `unavailable:${saved.id}`, href: '', kind: 'unavailable', minWidth: 320, saved });

/** Where a file view lives, for asking the server for its opaque reference. */
export function fileTarget(view: ViewReference): LayoutFile | null {
  if (view.kind !== 'file') return null;
  const url = new URL(view.href, 'https://example.lan');
  const path = url.searchParams.get('file'); if (!path) return null;
  const workspace = /^\/workspaces\/([^/]+)\/files$/.exec(url.pathname);
  if (workspace) return { location: 'workspace', id: url.searchParams.get('session') ?? '', folder: decodeURIComponent(workspace[1]), path };
  const match = /^\/(sessions|projects)\/([\w-]+)$/.exec(url.pathname);
  return match ? { location: match[1] === 'sessions' ? 'session' : 'project', id: match[2], folder: '', path } : null;
}

function saved(view: ViewReference, refs: ReadonlyMap<string, string>): LayoutView | null {
  if (view.kind === 'unavailable') return view.saved ?? null;
  if (view.kind === 'file') { const ref = refs.get(view.key); return ref ? { id: `f-${ref}`, kind: 'file', file_ref: ref } : null; }
  const url = new URL(view.href, 'https://example.lan');
  const match = /^\/(sessions|projects)\/([\w-]+)$/.exec(url.pathname); if (!match) return null;
  const session = match[1] === 'sessions';
  if (view.kind === 'terminal' || view.kind === 'chat') return { id: `${view.kind[0]}-${match[2]}`, kind: view.kind, resource: { kind: 'session', id: match[2] } };
  const tab = url.searchParams.get('tab') ?? undefined;
  if (tab !== undefined && !TAB.test(tab)) return null;
  return { id: `p${session ? 's' : 'p'}-${match[2]}${tab ? `-${tab}` : ''}`, kind: 'page', resource: session ? { kind: 'session', id: match[2] } : { kind: 'project', id: Number(match[2]) }, ...(tab ? { tab } : {}) };
}

/** A view this schema cannot name (a file with no reference yet, a workspace page) is left out and counted, not guessed at. */
export function toServer(layout: WorkingLayout, refs: ReadonlyMap<string, string>): { layout: Layout; omitted: number } {
  const ids = new Map<string, string>(), views: LayoutView[] = []; let omitted = 0;
  for (const [key, view] of Object.entries(layout.views)) {
    const item = saved(view, refs);
    if (!item || !ID.test(item.id) || views.some(other => other.id === item.id)) { omitted++; continue; }
    ids.set(key, item.id); views.push(item);
  }
  const columns = layout.columns.map(column => {
    const tabs = column.tabs.flatMap(key => ids.get(key) ?? []);
    return { tabs, active: (column.active && ids.get(column.active)) ?? tabs[0] ?? null, width: column.width };
  });
  const total = columns.reduce((sum, column) => sum + column.width, 0);
  return { omitted, layout: { schema_version: 2, views, focused: layout.focused, filled: layout.filled ? ids.get(layout.filled) ?? null : null,
    columns: columns.map(column => ({ ...column, width: column.width / total })) } };
}

function href(view: LayoutView, file?: LayoutFile): string | null {
  if (view.kind === 'file') {
    if (!file) return null;
    const query = (extra: Record<string, string>) => new URLSearchParams({ ...extra, file: file.path });
    if (file.location === 'workspace') return `/workspaces/${encodeURIComponent(file.folder)}/files?${query({ session: file.id })}`;
    return `/${file.location === 'session' ? 'sessions' : 'projects'}/${file.id}?${query({ tab: 'files' })}`;
  }
  const resource = view.resource; if (!resource) return null;
  if (resource.kind === 'project') return `/projects/${resource.id}${view.tab ? `?tab=${view.tab}` : ''}`;
  if (resource.kind === 'team') return `/sessions/${resource.id}?tab=team`;
  if (view.kind === 'terminal') return `/sessions/${resource.id}`;
  if (view.kind === 'chat') return `/sessions/${resource.id}?tab=chat`;
  return `/sessions/${resource.id}${view.tab ? `?tab=${view.tab}` : ''}`;
}

/** Restored references are untrusted: anything unresolved becomes a placeholder, and opening a resolved one still rechecks access. */
export function fromServer(record: Pick<LayoutRecord, 'layout' | 'resources'>, layout = record.layout): { layout: WorkingLayout | null; refs: Map<string, string> } {
  const refs = new Map<string, string>();
  if (!layout) return { layout: null, refs };
  const keys = new Map<string, string>(), views: Record<string, ViewReference> = {};
  for (const view of layout.views) {
    const resolved = record.resources[view.id];
    const target = resolved?.available === false ? null : href(view, resolved?.file);
    const reference = target ? referenceFor(target) : null;
    const item = reference && !views[reference.key] ? reference : placeholder(view);
    views[item.key] = item; keys.set(view.id, item.key);
    if (view.file_ref && reference) refs.set(reference.key, view.file_ref);
  }
  const tabsOf = (tabs: string[]) => tabs.flatMap(id => keys.get(id) ?? []);
  const columns = layout.columns.map(column => { const tabs = tabsOf(column.tabs); return { tabs, active: (column.active && keys.get(column.active)) ?? tabs[0] ?? null, width: column.width }; });
  return { refs, layout: { version: 1, views, columns, focused: Math.min(layout.focused, columns.length - 1), filled: layout.filled ? keys.get(layout.filled) ?? null : null } };
}

/** A copy kept offline restores its files as placeholders until a save returns their locations. Swaps only those,
 * in place: a reply can be older than the arrangement on screen, so it must never replace it.
 */
export function resolvePlaceholders(layout: WorkingLayout, resources: LayoutRecord['resources']): { layout: WorkingLayout; refs: Map<string, string> } {
  const refs = new Map<string, string>(), swapped = new Map<string, ViewReference>();
  for (const view of Object.values(layout.views)) {
    if (view.kind !== 'unavailable' || !view.saved?.file_ref) continue;
    const target = href(view.saved, resources[view.saved.id]?.file); const reference = target ? referenceFor(target) : null;
    if (reference && !layout.views[reference.key]) { swapped.set(view.key, reference); refs.set(reference.key, view.saved.file_ref); }
  }
  if (!swapped.size) return { layout, refs };
  const key = (value: string) => swapped.get(value)?.key ?? value;
  const views = Object.fromEntries(Object.entries(layout.views).map(([name, view]) => { const item = swapped.get(name) ?? view; return [item.key, item]; }));
  return { refs, layout: { ...layout, views, filled: layout.filled && key(layout.filled),
    columns: layout.columns.map(column => ({ ...column, tabs: column.tabs.map(key), active: column.active && key(column.active) })) } };
}

/** Taking another window's arrangement never hides work: views with drafts or control stay placed here. */
export function keepViews(incoming: WorkingLayout | null, current: WorkingLayout, keep: Iterable<string>): WorkingLayout {
  let next = incoming ?? emptyLayout();
  for (const key of keep) if (current.views[key] && !next.views[key]) next = openView(next, current.views[key]);
  return next;
}
