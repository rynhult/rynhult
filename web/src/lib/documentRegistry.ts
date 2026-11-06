export type FileRead = { content?: string; revision: string; size?: number; too_large?: boolean; binary?: boolean; media_type?: string; redacted?: boolean; file_identity?: string; workspace_revision?: string };
export type FileDocument = { opened: FileRead | null; draft: string; conflict: FileRead | null; editorVersion: number };
export const emptyDocument: FileDocument = { opened: null, draft: '', conflict: null, editorVersion: 0 };
/** One canonical editor per authorized physical placement. Never persisted to device storage. */
export class DocumentRegistry {
  private entries = new Map<string, FileDocument>();
  private identities = new Map<string, string>();
  private owners = new Map<string, string>();
  private listeners = new Set<() => void>();
  get size() { return this.entries.size; }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  get = (key: string) => this.entries.get(key) ?? emptyDocument;
  open(key: string) {
    if (this.entries.has(key)) return;
    // Eight base/draft pairs, each <= 1 Mi code units, bound text to <= 32 MiB.
    if (this.entries.size >= 8) throw new Error('Open file limit reached. Save and close a file before opening another.');
    this.entries.set(key, { ...emptyDocument });
  }
  bind(key: string, placement: string, generation: string) {
    const identity = JSON.stringify([placement, generation]);
    const previous = this.identities.get(key);
    if (previous && previous !== identity) throw new Error('The file placement changed. Reopen it before editing this checkout.');
    const owner = this.owners.get(identity);
    if (owner) return owner;
    this.identities.set(key, identity); this.owners.set(identity, key); return key;
  }
  update(key: string, patch: Partial<FileDocument>) {
    this.open(key);
    const next = { ...this.get(key), ...patch };
    if (next.draft.length > 1024 * 1024 || (next.opened?.content?.length ?? 0) > 1024 * 1024) throw new Error('File draft limit reached (1 Mi characters).');
    this.entries.set(key, next); this.listeners.forEach(listener => listener());
  }
  close(key: string) {
    this.entries.delete(key);
    const identity = this.identities.get(key); if (identity) this.owners.delete(identity);
    this.identities.delete(key); this.listeners.forEach(listener => listener());
  }
  clear() { this.entries.clear(); this.identities.clear(); this.owners.clear(); this.listeners.forEach(listener => listener()); }
}
