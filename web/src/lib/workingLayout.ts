import type { LayoutView } from './layoutStore.ts';
/** Versioned presentation only. Runtime resources, drafts and authority never enter this record. */
export type ViewReference = { key: string; href: string; kind: 'terminal' | 'file' | 'chat' | 'page' | 'unavailable'; minWidth: number;
  /** A saved reference that could not be resolved. Kept so a later save does not lose it; never rendered as content. */
  saved?: LayoutView };
export type Column = { tabs: string[]; active: string | null; width: number };
export type WorkingLayout = { version: 1; columns: Column[]; focused: number; filled: string | null; views: Record<string, ViewReference> };
export const MAX_VIEWS = 32;
const column = (): Column => ({ tabs: [], active: null, width: 1 });
export const emptyLayout = (): WorkingLayout => ({ version: 1, columns: [column()], focused: 0, filled: null, views: {} });
const cleanPath = (path: string) => !!path && path.length <= 2048 && !path.startsWith('/') && !path.includes('\\') && !/[\x00-\x1f\x7f]/.test(path) && path.split('/').every(part => part && part !== '.' && part !== '..');

/** Only resource routes can be restored. Other destinations retain their ordinary route flow. */
export function referenceFor(href: string): ViewReference | null {
  if (typeof href !== 'string' || href.length > 4096 || !href.startsWith('/') || href.startsWith('//')) return null;
  try {
    const url = new URL(href, 'https://example.lan');
    const match = /^\/(sessions|projects)\/([\w-]+)$/.exec(url.pathname);
    const workspace = /^\/workspaces\/([^/]+)\/files$/.exec(url.pathname);
    if (!match && !workspace || workspace && !url.searchParams.get('session')) return null;
    if ([...url.searchParams.keys()].some(key => !['tab', 'file', 'session', 'delivery'].includes(key))) return null;
    if ([...url.searchParams.keys()].some(key => url.searchParams.getAll(key).length !== 1)) return null;
    const tab = url.searchParams.get('tab') ?? (match?.[1] === 'sessions' ? 'terminal' : 'overview');
    const file = url.searchParams.get('file');
    if (file !== null && !cleanPath(file)) return null;
    if (file && (tab === 'files' || workspace)) {
      const kind = workspace ? 'workspace' : match![1] === 'sessions' ? 'session' : 'project';
      const id = workspace ? url.searchParams.get('session') : match![2];
      if (!id || !/^[\w-]+$/.test(id)) return null;
      const query = new URLSearchParams(workspace ? { session: id, file } : { tab: 'files', file });
      return { key: `file:${kind}:${id}:${file}`, href: `${url.pathname}?${query}`, kind: 'file', minWidth: 360 };
    }
    if (match?.[1] === 'sessions' && tab === 'terminal') return { key: `session:${match[2]}:terminal`, href: url.pathname, kind: 'terminal', minWidth: 320 };
    if (match?.[1] === 'sessions' && tab === 'chat') return { key: `session:${match[2]}:chat`, href: `${url.pathname}?tab=chat`, kind: 'chat', minWidth: 360 };
    const canonical = `${url.pathname}${url.search}${url.hash}`;
    return { key: `page:${canonical}`, href: canonical, kind: 'page', minWidth: tab === 'preview' ? 400 : tab === 'files' ? 360 : 320 };
  } catch { return null; }
}

const copy = (layout: WorkingLayout): WorkingLayout => ({ ...layout, columns: layout.columns.map(c => ({ ...c, tabs: [...c.tabs] })), views: { ...layout.views } });
export function openView(layout: WorkingLayout, view: ViewReference): WorkingLayout {
  const next = copy(layout);
  const existing = next.columns.findIndex(c => c.tabs.includes(view.key));
  if (existing >= 0) { next.focused = existing; next.columns[existing].active = view.key; next.filled = null; return next; }
  if (Object.keys(next.views).length >= MAX_VIEWS) throw new Error('The open view limit is reached. Close a view before opening another.');
  next.views[view.key] = view;
  next.columns[next.focused].tabs.push(view.key); next.columns[next.focused].active = view.key;
  next.filled = null;
  return next;
}
export function moveView(layout: WorkingLayout, key: string, destination: number, before?: string): WorkingLayout {
  if (!layout.views[key] || !Number.isInteger(destination) || destination < 0 || destination > 2) return layout;
  const next = copy(layout);
  while (next.columns.length <= destination) next.columns.push(column());
  for (const c of next.columns) {
    const index = c.tabs.indexOf(key);
    if (index >= 0) { c.tabs.splice(index, 1); if (c.active === key) c.active = c.tabs[Math.min(index, c.tabs.length - 1)] ?? null; }
  }
  const target = next.columns[destination]; const index = before ? target.tabs.indexOf(before) : -1;
  target.tabs.splice(index < 0 ? target.tabs.length : index, 0, key); target.active = key;
  next.focused = destination; next.filled = null;
  if (next.columns.length !== layout.columns.length) for (const c of next.columns) c.width = 1 / next.columns.length;
  return next;
}
export function closeView(layout: WorkingLayout, key: string): WorkingLayout {
  const next = copy(layout); delete next.views[key];
  for (const c of next.columns) {
    const index = c.tabs.indexOf(key); if (index < 0) continue;
    c.tabs.splice(index, 1); if (c.active === key) c.active = c.tabs[Math.min(index, c.tabs.length - 1)] ?? null;
  }
  if (next.filled === key) next.filled = null;
  return next;
}
export const columnMinimum = (layout: WorkingLayout, c: Column) => Math.max(320, ...c.tabs.map(key => layout.views[key]?.minWidth ?? 320));
export function effectiveColumns(layout: WorkingLayout, width: number): number {
  return layout.filled || width < layout.columns.reduce((n, c) => n + columnMinimum(layout, c), 0) + (layout.columns.length - 1) * 8 ? 1 : layout.columns.length;
}
/** Normalize saved proportions against current minimum widths, then resize a neighbouring pair. */
export function resizeColumns(layout: WorkingLayout, divider: number, delta: number, width: number): WorkingLayout {
  const next = copy(layout); const usable = width - (next.columns.length - 1) * 8;
  const minima = next.columns.map(c => columnMinimum(next, c)); const extra = usable - minima.reduce((n, value) => n + value, 0);
  if (extra < 0) return layout;
  const spare = next.columns.map((c, i) => Math.max(0, c.width * usable - minima[i]));
  const sum = spare.reduce((n, value) => n + value, 0);
  const sizes = minima.map((min, i) => min + extra * (sum ? spare[i] / sum : 1 / minima.length));
  if (divider >= 0 && divider < sizes.length - 1) {
    const change = Math.max(minima[divider] - sizes[divider], Math.min(delta, sizes[divider + 1] - minima[divider + 1]));
    sizes[divider] += change; sizes[divider + 1] -= change;
  }
  next.columns.forEach((c, i) => { c.width = sizes[i] / usable; });
  return next;
}
