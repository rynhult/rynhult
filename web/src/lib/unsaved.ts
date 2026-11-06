/** Tab-local ownership of unsaved work. Entries contain no authority to save/launch.
 * Public snapshots contain descriptions, never draft bodies or credentials. */
import { draftIdentity, onDraftIdentity, type DraftScope } from './draftIdentity.ts';
export type { DraftScope } from './draftIdentity.ts';
export type DraftKind = 'files' | 'brief' | 'security' | 'remote-access' | 'publishing' | 'publishing-address' | 'new-project' | 'new-session' | 'worklist' | 'plan' | 'register-folder';
export interface UnsavedDraft {
  id: string; kind: DraftKind; label: string; value?: unknown; base?: unknown; parent?: string;
  persistence?: 'memory' | 'local' | 'failed'; problem?: 'remove'; discard: () => void; flush?: () => boolean;
}
export type DraftSummary = Pick<UnsavedDraft, 'id' | 'kind' | 'label' | 'persistence' | 'parent' | 'problem'> & { scope: DraftScope | null; version: number };
export class UnsavedWorkRegistry {
  private scope: DraftScope | null = null;
  private entries = new Map<string, UnsavedDraft & { version: number }>();
  private listeners = new Set<() => void>();
  private snapshot: DraftSummary[] = [];
  private listening = false;
  private revision = 0;
  private warn = (event: BeforeUnloadEvent) => { if (this.entries.size) { event.preventDefault(); event.returnValue = ''; } };
  getScope = () => this.scope;
  setScope(scope: DraftScope | null) {
    if (JSON.stringify(this.scope) === JSON.stringify(scope)) return;
    this.scope = scope; this.entries.clear(); this.publish();
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.snapshot;
  private publish() {
    this.snapshot = [...this.entries.values()].map(({ id, kind, label, persistence, parent, problem, version }) =>
      ({ id, kind, label, persistence, parent, problem, version, scope: this.scope ? { ...this.scope } : null }));
    if (typeof window !== 'undefined') {
      if (this.entries.size && !this.listening) { window.addEventListener('beforeunload', this.warn); this.listening = true; }
      if (!this.entries.size && this.listening) { window.removeEventListener('beforeunload', this.warn); this.listening = false; }
    }
    this.listeners.forEach(listener => listener());
  }
  register(entry: UnsavedDraft): boolean {
    if (!this.scope) return false;
    this.entries.set(entry.id, { ...entry, persistence: entry.persistence ?? 'memory', version: ++this.revision });
    this.publish(); return true;
  }
  registerVolatile(entry: UnsavedDraft): boolean {
    if (this.scope) return false;
    this.entries.set(entry.id, { ...entry, persistence: 'failed', version: ++this.revision });
    this.publish(); return true;
  }
  read<T>(id: string): T | undefined { return this.entries.get(id)?.value as T | undefined; }
  unregister(id: string) { if (this.entries.delete(id)) this.publish(); }
  discard(id: string, expectedVersion?: number): boolean {
    const entry = this.entries.get(id);
    if (!entry || expectedVersion !== undefined && entry.version !== expectedVersion) return false;
    entry.discard();
    if (this.entries.get(id) === entry) this.unregister(id);
    return true;
  }
  flush(id: string, expectedVersion?: number): boolean {
    const entry = this.entries.get(id);
    if (!entry || expectedVersion !== undefined && entry.version !== expectedVersion) return false;
    return entry.flush?.() ?? false;
  }
}
export const unsavedWork = new UnsavedWorkRegistry();
unsavedWork.setScope(draftIdentity());
onDraftIdentity(scope => unsavedWork.setScope(scope));

/** Restore only from the active person's memory; never from another scope. */
export function draftField<T>(id: string, field: string, fallback: T): T {
  return (unsavedWork.read<Record<string, unknown>>(id)?.[field] as T | undefined) ?? fallback;
}
