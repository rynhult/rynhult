/** Presentation-only layout persistence. The shell supplies api() and renders the returned states.
 * Never auto-save on load or auto-resolve a conflict. Dispose on sign-out/identity change.
 */
export type LayoutScope = {
  installation: string; person: string; project_id: number;
  device: 'desktop' | 'tablet' | 'phone'; viewport: 'wide' | 'medium' | 'narrow';
};
export type LayoutView = { id: string; kind: 'terminal' | 'chat' | 'file' | 'page';
  resource?: { kind: 'project' | 'session' | 'team'; id: string | number }; tab?: string; file_ref?: string };
export type LayoutColumn = { tabs: string[]; active: string | null; width: number };
export type Layout = {
  schema_version: 2; views: LayoutView[]; columns: LayoutColumn[]; focused: number; filled: string | null;
  sidebar?: { collapsed?: boolean }; header?: { compact?: boolean };
};
export type LayoutFile = { location: 'project' | 'session' | 'workspace'; id: string; folder: string; path: string };
export type LayoutRecord = {
  scope: LayoutScope; revision: number; schema_version: number; layout: Layout | null;
  resources: Record<string, { available: boolean; file?: LayoutFile }>;
};
type Pending = { expected_revision: number; layout: Layout | null };
export type LayoutState = {
  status: 'ready' | 'saved' | 'offline' | 'pending' | 'conflict' | 'refused';
  server: LayoutRecord | null; pending: Pending | null; durable: boolean; note?: string;
};
export type LayoutTransport = (path: string, init?: { method?: string; json?: unknown }) => Promise<LayoutRecord>;

const idPattern = /^[A-Za-z0-9_-]{1,64}$/;
const tabPattern = /^[a-z][a-z-]{0,31}$/;
export const PENDING_PREFIX = 'ryn-layout-pending:v2:';
function clean(value: Layout): Layout {
  // Mirror the server's closed presentation schema, including dropping extensions.
  if (value.schema_version !== 2 || !Array.isArray(value.views) || value.views.length > 32
      || !Array.isArray(value.columns) || !value.columns.length || value.columns.length > 3) throw Error('Unsupported layout');
  const ids = new Set<string>();
  const views = value.views.map(view => {
    if (typeof view.id !== 'string' || !idPattern.test(view.id) || ids.has(view.id)) throw Error('Invalid view id');
    ids.add(view.id);
    if (!['terminal', 'chat', 'file', 'page'].includes(view.kind)) throw Error('Invalid view kind');
    const out: LayoutView = { id: view.id, kind: view.kind };
    if (view.kind === 'file') {
      if (typeof view.file_ref !== 'string' || !idPattern.test(view.file_ref)) throw Error('Invalid file reference');
      out.file_ref = view.file_ref;
      return out;
    }
    if (view.resource) {
      const { kind, id } = view.resource;
      if (!['project', 'session', 'team'].includes(kind) || (kind === 'project'
        ? !Number.isSafeInteger(id) || Number(id) < 1
        : typeof id !== 'string' || !idPattern.test(id))) throw Error('Invalid resource reference');
      out.resource = { kind, id };
    }
    if (view.kind === 'page' && view.tab !== undefined) {
      if (typeof view.tab !== 'string' || !tabPattern.test(view.tab)) throw Error('Invalid tab');
      out.tab = view.tab;
    }
    return out;
  });
  const placed = new Set<string>();
  const columns = value.columns.map(column => {
    if (!Array.isArray(column.tabs) || typeof column.width !== 'number' || !(column.width > 0 && column.width <= 1)) throw Error('Invalid column');
    for (const tab of column.tabs) { if (!ids.has(tab) || placed.has(tab)) throw Error('Invalid column tab'); placed.add(tab); }
    if (column.active !== null && !column.tabs.includes(column.active) || column.tabs.length && column.active === null) throw Error('Invalid active tab');
    return { tabs: [...column.tabs], active: column.active, width: column.width };
  });
  if (placed.size !== ids.size || !Number.isInteger(value.focused) || value.focused < 0 || value.focused >= columns.length
      || value.filled !== null && !placed.has(value.filled)) throw Error('Invalid layout placement');
  const out: Layout = { schema_version: 2, views, columns, focused: value.focused, filled: value.filled };
  if (value.sidebar) {
    if (value.sidebar.collapsed !== undefined && typeof value.sidebar.collapsed !== 'boolean') throw Error('Invalid sidebar');
    out.sidebar = value.sidebar.collapsed === undefined ? {} : { collapsed: value.sidebar.collapsed };
  }
  if (value.header) {
    if (value.header.compact !== undefined && typeof value.header.compact !== 'boolean') throw Error('Invalid header');
    out.header = value.header.compact === undefined ? {} : { compact: value.header.compact };
  }
  if (new TextEncoder().encode(JSON.stringify(out)).length > 30000) throw Error('Layout too large');
  return out;
}

/** The arrangement itself: key order and width rounding are not edits, and flags the shell doesn't set are not compared. */
const shape = (value: Layout) => JSON.stringify([
  value.views.map(view => [view.id, view.kind, view.resource?.kind, view.resource?.id, view.tab, view.file_ref]),
  value.columns.map(column => [column.tabs, column.active, Math.round(column.width * 1e6)]), value.focused, value.filled]);
export const sameLayout = (one: Layout | null | undefined, two: Layout | null | undefined) =>
  one && two ? shape(one) === shape(two) : !one && !two;

function scopeKey(scope: LayoutScope): string {
  return JSON.stringify([scope.installation, scope.person, scope.project_id, scope.device, scope.viewport]);
}

export class PersonalLayoutStore {
  private scope: LayoutScope;
  private transport: LayoutTransport;
  private storage: Storage | undefined;
  private key: string;
  private server: LayoutRecord | null = null;
  private pending: Pending | null = null;
  private conflict = false;
  private durable = false;
  private disposed = false;
  private busy = false;
  private storageFailure = false;
  private unreadable = false;
  private retained: string | null = null;

  constructor(scope: LayoutScope, transport: LayoutTransport, storage?: Storage) {
    if (!Number.isSafeInteger(scope.project_id) || scope.project_id < 1
        || !['desktop', 'tablet', 'phone'].includes(scope.device) || !['wide', 'medium', 'narrow'].includes(scope.viewport)
        || !scope.installation || !scope.person || scope.installation.length > 64 || scope.person.length > 64) throw Error('Invalid layout scope');
    this.scope = { ...scope };
    this.transport = transport;
    this.storage = storage;
    this.key = PENDING_PREFIX + scopeKey(scope);
    try {
      if (!this.storage && typeof window !== 'undefined') this.storage = window.localStorage;
      const raw = this.storage?.getItem(this.key);
      this.retained = raw ?? null;
      if (raw) {
        if (raw.length > 32768) throw Error('Pending layout too large');
        const value = JSON.parse(raw);
        if (!Number.isSafeInteger(value.expected_revision) || value.expected_revision < 0) throw Error('Invalid revision');
        this.pending = { expected_revision: value.expected_revision, layout: value.layout === null ? null : clean(value.layout) };
        this.durable = true;
      }
    } catch { this.storageFailure = true; this.unreadable = true; }
  }

  private path(): string {
    return `/api/projects/${this.scope.project_id}/layouts/${this.scope.device}/${this.scope.viewport}`;
  }
  private active(): void { if (this.disposed) throw Error('Layout store disposed'); }
  private state(status: LayoutState['status'], note?: string): LayoutState {
    // Give consumers copies; mutating a rendered state cannot change a CAS body.
    return structuredClone({ status, server: this.server, pending: this.pending, durable: this.durable,
      note: note ?? (this.storageFailure ? 'Local layout storage is unavailable. Keep this page open.' : undefined) });
  }
  private accept(record: LayoutRecord): void {
    if (scopeKey(record.scope) !== scopeKey(this.scope) || !Number.isSafeInteger(record.revision) || record.revision < 0
        || record.schema_version !== 2) throw Error('Unsupported layout response; reload with a compatible client');
    this.server = structuredClone(record);
  }
  private persist(): void {
    try {
      if (!this.storage) throw Error('No local storage');
      if (this.storage.getItem(this.key) !== this.retained) throw Error('Another tab changed the retained layout');
      if (this.pending) {
        if (this.unreadable) throw Error('The retained layout could not be read; do not overwrite it');
        // Bound offline pending records; never evict someone else's unsaved arrangement.
        const prefix = PENDING_PREFIX;
        let count = 0;
        for (let i = 0; i < this.storage.length; i++) if (this.storage.key(i)?.startsWith(prefix)) count++;
        if (!this.storage.getItem(this.key) && count >= 32) throw Error('Pending layout store full');
        this.storage.setItem(this.key, JSON.stringify(this.pending));
      } else this.storage.removeItem(this.key);
      this.retained = this.pending ? JSON.stringify(this.pending) : null;
      this.durable = !!this.pending;
      this.storageFailure = false;
    } catch { this.durable = false; this.storageFailure = true; }
  }
  private failure(error: unknown): LayoutState {
    const e = error as { status?: number; data?: { current?: LayoutRecord } };
    if (e.status === 409 && e.data?.current) {
      this.accept(e.data.current);
      // The server already holds exactly this arrangement: a reply was lost, not another window's change.
      if (this.pending && sameLayout(this.pending.layout, e.data.current.layout)) {
        this.pending = null; this.persist();
        return this.state('saved');
      }
      this.conflict = true;
      return this.state('conflict', 'The server layout changed. Choose the server arrangement or your pending arrangement.');
    }
    if (e.status === 401 || e.status === 403 || e.status === 404) {
      this.server = null;
      this.pending = null;
      this.persist();
      return this.state('refused', 'Sign in and check access before loading this layout again.');
    }
    if (e.status || !(error instanceof TypeError)) return this.state('refused', 'The server refused this layout. Your pending arrangement is kept.');
    if (this.server) this.server.resources = Object.fromEntries(Object.keys(this.server.resources).map(id => [id, { available: false }]));
    return this.state('offline', this.storageFailure ? 'Offline; local storage unavailable. Keep this page open.'
      : 'Could not reach the server. No automatic retry; your pending arrangement is kept.');
  }

  async load(): Promise<LayoutState> {
    this.active();
    if (this.busy) throw Error('A layout request is already running');
    this.busy = true;
    try {
      const record = await this.transport(this.path());
      this.active();
      this.accept(record);
      if (this.pending && sameLayout(this.pending.layout, record.layout)) { this.pending = null; this.persist(); }
      this.conflict = !!this.pending && this.pending.expected_revision !== record.revision;
      return this.state(this.conflict ? 'conflict' : 'ready');
    } catch (error) { this.active(); return this.failure(error); }
    finally { this.busy = false; }
  }

  current(): LayoutState { return this.state(this.conflict ? 'conflict' : 'ready'); }

  /** True when an offline copy from an earlier visit waits to be sent or reviewed. */
  get hasPending(): boolean { return !!this.pending; }

  stage(layout: Layout | null): LayoutState {
    this.active();
    if (!this.server) throw Error('Load the server layout before editing');
    this.pending = { expected_revision: this.pending?.expected_revision ?? this.server.revision,
      layout: layout === null ? null : clean(layout) };
    this.persist();
    return this.state(this.conflict ? 'conflict' : 'pending');
  }

  async save(): Promise<LayoutState> {
    this.active();
    if (this.busy) throw Error('A layout request is already running');
    if (this.conflict) return this.state('conflict');
    if (!this.pending || !this.server) throw Error('Load and stage a layout first');
    const snapshot = this.pending;
    this.busy = true;
    try {
      const result = await this.transport(this.path() + (snapshot.layout === null ? '/reset' : ''),
        { method: snapshot.layout === null ? 'POST' : 'PUT', json: snapshot });
      this.active();
      this.accept(result);
      if (this.pending === snapshot) this.pending = null;
      else if (this.pending) this.pending = { ...this.pending, expected_revision: result.revision };
      this.persist();
      return this.state(this.pending ? 'pending' : 'saved');
    } catch (error) { this.active(); return this.failure(error); }
    finally { this.busy = false; }
  }

  /** Choosing local is explicit permission for the next CAS, not an unconditional overwrite. */
  resolve(choice: 'server' | 'local'): LayoutState {
    this.active();
    if (!this.conflict || !this.server || !this.pending) throw Error('No layout conflict to resolve');
    if (choice === 'server') this.pending = null;
    else this.pending = { ...this.pending, expected_revision: this.server.revision };
    this.conflict = false;
    this.persist();
    return this.state(this.pending ? 'pending' : 'ready');
  }

  /** Stop using this store and reject late replies, leaving a retained copy for the next load (the view is closing, not signing out). */
  detach(): void { this.disposed = true; }

  /** Call on sign-out, identity switch, project deletion or authorization revocation. */
  dispose(): void {
    this.pending = null;
    this.server = null;
    this.persist();
    this.disposed = true;
  }
}
