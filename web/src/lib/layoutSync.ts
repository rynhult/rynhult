import { PENDING_PREFIX, PersonalLayoutStore, sameLayout, type Layout, type LayoutRecord, type LayoutScope, type LayoutState, type LayoutTransport } from './layoutStore.ts';

export type SyncStatus = 'loading' | 'synced' | 'saving' | 'offline' | 'conflict' | 'refused' | 'local';
export type SyncView = { status: SyncStatus; durable: boolean; note?: string };
type Start = { record: LayoutRecord | null; pending?: Layout | null };
type Deps = { transport: LayoutTransport; storage?: Storage; onChange: (view: SyncView) => void; onSaved?: (record: LayoutRecord) => void;
  onScope?: (scope: LayoutScope) => void; delay?: number; retry?: number };
const VISIT = 'Columns here last for this visit.';

/** Copies of another person, or of a project no longer in this person's inventory, must not wait to be sent. Old device records go too. */
export function prunePendingLayouts(installation: string, person: string, projects?: Set<number>) {
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('rynhult:working:') || key.startsWith('ryn-layout-pending:') && !key.startsWith(PENDING_PREFIX)) { localStorage.removeItem(key); continue; }
      if (!key.startsWith(PENDING_PREFIX)) continue;
      let identity: unknown[] = []; try { identity = JSON.parse(key.slice(PENDING_PREFIX.length)); } catch { /* Not ours to read: treated as foreign below. */ }
      if (!Array.isArray(identity) || identity[0] === installation && (identity[1] !== person || !!projects && !projects.has(identity[2] as number))) localStorage.removeItem(key);
    }
  } catch { /* Unavailable browser storage cannot be read or erased by this page. */ }
}

/** One project's arrangement in one window. Saves only what the shell hands it, always by compare-and-swap, and
 * never settles a conflict itself. Knows nothing of terminals, drafts or sessions: nothing here can start work.
 */
export class LayoutSync {
  private store: PersonalLayoutStore | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private running = false; private closed = false; private again = false;
  /** Changes made before the server could be reached: a later server layout was never seen by this window. */
  private unseen: { layout: Layout | null } | null = null;
  private recoverable = false;
  private online = () => { void this.retry(); };
  view: SyncView = { status: 'loading', durable: false };

  private path: string; private deps: Deps;

  constructor(path: string, deps: Deps) {
    this.path = path; this.deps = deps;
    if (typeof window !== 'undefined') window.addEventListener('online', this.online);
  }
  private set(view: SyncView) { this.view = view; if (!this.closed) this.deps.onChange(view); }
  private show(state: LayoutState) {
    this.set({ note: state.note, durable: state.durable, status: state.status === 'conflict' ? 'conflict' : state.status === 'offline' ? 'offline'
      : state.status === 'refused' ? 'refused' : state.status === 'pending' ? 'saving' : 'synced' });
    if (state.status === 'offline') this.later(this.deps.retry ?? 15000);
    return state;
  }
  private later(delay: number) { clearTimeout(this.timer); if (!this.closed) this.timer = setTimeout(() => void this.retry(), delay); }

  /** Never writes. A copy kept from an earlier visit is returned for display, then sent unless it now conflicts. */
  async start(): Promise<Start> {
    let first: LayoutRecord;
    try { first = await this.deps.transport(this.path); }
    catch (error) {
      if (this.closed) return { record: null };
      this.recoverable = !(error as { status?: number }).status;
      this.set({ status: 'local', durable: false, note: this.recoverable ? 'Could not reach the server. Columns will last for this visit only until it returns.' : VISIT });
      if (this.recoverable) this.later(this.deps.retry ?? 15000);
      return { record: null };
    }
    if (this.closed) return { record: null };
    this.deps.onScope?.(first.scope);
    this.store = new PersonalLayoutStore(first.scope, this.deps.transport, this.deps.storage);
    const state = this.show(await this.store.load());
    if (this.closed || !state.server) return { record: null };
    if (!state.pending) return { record: state.server };
    if (state.status !== 'conflict') { this.set({ ...this.view, status: 'saving' }); this.later(0); }
    return { record: state.server, pending: state.pending.layout };
  }

  change(layout: Layout | null) {
    if (this.closed) return;
    if (!this.store) { if (this.recoverable) this.unseen = { layout }; return; }
    if (this.unseen) { this.unseen = { layout }; return; }
    const state = this.store.current();
    // Nothing changed: a write here would only raise a false conflict in another window.
    if (state.server && sameLayout(layout, state.pending ? state.pending.layout : state.server.layout)) return;
    try { this.show(this.store.stage(layout)); }
    catch (error) { this.set({ status: 'refused', durable: false, note: (error as Error).message }); return; }
    if (this.view.status !== 'conflict') this.later(this.deps.delay ?? 400);
  }

  private async flush() {
    if (this.closed || !this.store?.hasPending || this.view.status === 'conflict') return;
    if (this.running) { this.again = true; return; }
    this.running = true;
    try {
      const state = this.show(await this.store.save());
      if (state.server && state.status === 'saved') this.deps.onSaved?.(state.server);
    } catch { /* Closed while the reply was in flight. */ } finally { this.running = false; }
    if (this.again || this.view.status === 'saving') { this.again = false; this.later(0); }
  }

  /** The only retries: reconnection, the offline timer, or an explicit choice. Each is still a compare-and-swap. */
  async retry() {
    if (this.closed) return;
    if (this.store) return this.flush();
    if (!this.recoverable || this.running) return;
    this.running = true;
    try {
      const first = await this.deps.transport(this.path);
      if (this.closed) return;
      this.deps.onScope?.(first.scope);
      this.store = new PersonalLayoutStore(first.scope, this.deps.transport, this.deps.storage);
      const state = this.show(await this.store.load());
      if (this.closed) return;
      if (!this.unseen) return;
      if (state.server?.layout) { this.set({ status: 'conflict', durable: false, note: undefined }); return; }
      const { layout } = this.unseen; this.unseen = null;
      this.running = false; this.change(layout);
    } catch { this.later(this.deps.retry ?? 15000); } finally { this.running = false; }
  }

  chooseThisWindow() {
    if (!this.store) return;
    if (this.unseen) { const { layout } = this.unseen; this.unseen = null; this.set({ status: 'saving', durable: false }); this.change(layout); return; }
    this.show(this.store.resolve('local')); this.later(0);
  }

  /** The newer record, settling nothing: if the caller can't place it, the conflict must stay open. */
  newer(): LayoutRecord | null { return this.store?.current().server ?? null; }

  /** Call only once the record can be shown. The caller keeps views holding drafts or control. */
  chooseNewer(): LayoutRecord | null {
    if (!this.store) return null;
    this.unseen = null;
    const state = this.store.current();
    return this.show(state.status === 'conflict' ? this.store.resolve('server') : state).server;
  }

  /** Leaves any retained copy for the next visit; sign-out clears those separately. */
  close() {
    this.closed = true; clearTimeout(this.timer);
    if (typeof window !== 'undefined') window.removeEventListener('online', this.online);
    this.store?.detach();
  }
}
