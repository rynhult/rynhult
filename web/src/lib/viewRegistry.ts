/** Browser-tab resource ownership. Layout references never grant server authority. */
export type WriterPolicy = 'p1-claim-if-free' | 'p2-observer';
export type TerminalReference = Readonly<{
  installation: string; person: string; sessionId: string; runId: string; node: string; account: string;
}>;
export type Pins = { dirty: boolean; unsent: boolean; uncertain: boolean };
export interface TerminalResource<Slot> {
  readonly writerToken: object | null;
  readonly composing: boolean;
  readonly state: 'connecting' | 'open' | 'closed';
  place(slot: Slot | null): void;
  setVisibility(visible: boolean): void;
  dispose(): void;
  suspend?(): unknown;
  restore?(state: unknown): void;
}
export type DisposeReason = 'release' | 'discard-input' | 'permission-revoked' | 'sign-out' | 'shutdown';
export function terminalKey(reference: TerminalReference): string {
  const fields = [reference.installation, reference.person, reference.sessionId, reference.runId, reference.node, reference.account];
  if (fields.some(value => typeof value !== 'string' || !value.trim())) throw new Error('Incomplete terminal identity.');
  return JSON.stringify(fields);
}
type View = { key: string; visible: boolean; slot?: string };
type Entry<Slot> = {
  reference: TerminalReference; resource: TerminalResource<Slot> | null; views: Set<string>;
  evictWhenUnpinned?: boolean; waiting?: boolean; savedState?: unknown; canonical?: string; hiddenSince: number; pins: Pins; reason?: string;
};

export class ViewRegistry<Slot> {
  private entries = new Map<string, Entry<Slot>>();
  private views = new Map<string, View>();
  private slots = new Map<string, Slot>();
  private sequence = 0;
  private stopped = false;
  private now: () => number;
  private create: (reference: TerminalReference, policy: WriterPolicy) => TerminalResource<Slot>;
  private policy: WriterPolicy;
  private maxAttached: number;
  private maxVisible: number;
  private idleMs: number;

  constructor(options: {
    create: (reference: TerminalReference, policy: WriterPolicy) => TerminalResource<Slot>;
    writerPolicy?: WriterPolicy; now?: () => number; maxAttached?: number; maxVisible?: number; idleMs?: number;
  }) {
    this.create = options.create; this.policy = options.writerPolicy ?? 'p2-observer'; this.now = options.now ?? (() => performance.now());
    this.maxAttached = options.maxAttached ?? 8; this.maxVisible = options.maxVisible ?? 4; this.idleMs = options.idleMs ?? 600_000;
    if (![this.maxAttached, this.maxVisible, this.idleMs].every(value => Number.isSafeInteger(value) && value > 0)) throw new Error('Invalid registry budget.');
    if (!['p1-claim-if-free', 'p2-observer'].includes(this.policy)) throw new Error('Invalid writer policy.');
  }

  registerSlot(id: string, slot: Slot): () => void {
    this.running();
    if (this.slots.has(id)) throw new Error('Slot already registered.');
    this.slots.set(id, slot);
    let registered = true;
    return () => {
      if (!registered) return;
      registered = false; this.slots.delete(id);
      for (const [viewId, view] of this.views) if (view.slot === id) {
        this.setVisibility(viewId, false); view.slot = undefined;
        const entry = this.entries.get(view.key)!;
        if (entry.canonical === viewId) entry.resource?.place(null);
      }
    };
  }

  acquire(reference: TerminalReference): { viewId: string; resourceKey: string } {
    this.running();
    const key = terminalKey(reference);
    let entry = this.entries.get(key);
    if (!entry?.resource) this.sweep();
    if (!entry?.resource) {
      const room = this.makeRoom();
      if (!entry) {
        entry = { reference: Object.freeze({ ...reference }), resource: null, views: new Set(), hiddenSince: this.now(), pins: { dirty: false, unsent: false, uncertain: false } };
        this.entries.set(key, entry);
      }
      entry.waiting = !room;
      try {
        if (room) entry.resource = this.create(entry.reference, this.policy);
        entry.resource?.restore?.(entry.savedState);
        entry.resource?.setVisibility(false); entry.hiddenSince = this.now(); entry.reason = undefined;
      } catch {
        entry.resource?.dispose(); entry.resource = null;
        entry.reason = 'Terminal attachment unavailable.';
      }
    }
    const viewId = `view-${++this.sequence}`;
    entry.views.add(viewId); this.views.set(viewId, { key, visible: false });
    return { viewId, resourceKey: key };
  }

  place(viewId: string, slotId: string): void {
    this.running();
    const [view, entry] = this.lookup(viewId);
    if (!this.slots.has(slotId)) throw new Error('Unknown terminal slot.');
    for (const other of this.entries.values()) {
      const canonical = other.canonical && this.views.get(other.canonical);
      if (other !== entry && canonical && canonical.visible && canonical.slot === slotId) throw new Error('Terminal slot occupied.');
    }
    this.checkVisible(entry);
    if (!entry.resource) {
      if (!this.makeRoom()) { entry.waiting = true; view.slot = slotId; view.visible = true; return; }
      entry.waiting = false;
      entry.resource = this.create(entry.reference, this.policy);
      entry.resource.restore?.(entry.savedState);
      entry.reason = undefined;
    }
    if (entry.canonical && entry.canonical !== viewId) this.views.get(entry.canonical)!.visible = false;
    entry.canonical = viewId; view.slot = slotId; view.visible = true;
    entry.resource.place(this.slots.get(slotId)!); entry.resource.setVisibility(true);
  }

  setVisibility(viewId: string, visible: boolean): void {
    const [view, entry] = this.lookup(viewId);
    if (visible) {
      if (!view.slot || !this.slots.has(view.slot)) throw new Error('Place the view in a registered slot first.');
      this.place(viewId, view.slot); return;
    }
    const wasVisible = this.visible(entry);
    view.visible = false;
    if (entry.canonical === viewId) entry.resource?.setVisibility(false);
    if (wasVisible && !this.visible(entry)) entry.hiddenSince = this.now();
    this.trimOverflow();
  }

  closeView(viewId: string): void {
    const view = this.views.get(viewId);
    if (!view) return;
    const entry = this.entries.get(view.key)!;
    this.setVisibility(viewId, false);
    if (entry.canonical === viewId) { entry.canonical = undefined; entry.resource?.place(null); }
    this.views.delete(viewId); entry.views.delete(viewId);
    if (!entry.views.size && !entry.resource && !this.pinned(entry)) this.entries.delete(view.key);
  }

  inspect(key: string) {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    return {
      reference: { ...entry.reference }, views: entry.views.size, canonicalView: entry.canonical,
      visible: this.visible(entry), waiting: !!entry.waiting, state: entry.resource?.state ?? 'detached', reason: entry.reason,
      writer: !!entry.resource?.writerToken, composing: !!entry.resource?.composing,
      pins: { ...entry.pins }, hiddenSince: entry.hiddenSince,
    };
  }

  setPins(key: string, pins: Partial<Pins>): void {
    const entry = this.entries.get(key);
    if (!entry) throw new Error('Unknown terminal resource.');
    for (const name of ['dirty', 'unsent', 'uncertain'] as const) if (pins[name] !== undefined) entry.pins[name] = !!pins[name];
    this.trimOverflow();
  }

  sweep(): void {
    this.trimOverflow();
    const now = this.now();
    const candidates = [...this.entries.values()].filter(entry => entry.resource && !this.visible(entry) && !this.pinned(entry) && now - entry.hiddenSince >= this.idleMs)
      .sort((a, b) => a.hiddenSince - b.hiddenSince);
    for (const entry of candidates) {
      const resource = entry.resource!; entry.resource = null;
      entry.reason = 'Terminal detached after inactivity. Recheck access before reconnecting.';
      resource.dispose();
      if (!entry.views.size) this.entries.delete(terminalKey(entry.reference));
    }
    // Retry deferred views within the normal budget on the existing owner sweep.
    for (const entry of this.entries.values()) {
      if (!entry.waiting || entry.resource || this.attached() >= this.maxAttached) continue;
      try {
        entry.resource = this.create(entry.reference, this.policy);
        entry.resource.restore?.(entry.savedState); entry.resource.setVisibility(false);
        entry.waiting = false; entry.reason = undefined;
        for (const id of entry.views) {
          const view = this.views.get(id)!;
          if (view.visible && view.slot && this.slots.has(view.slot)) { this.place(id, view.slot); break; }
        }
      } catch {
        entry.resource?.dispose(); entry.resource = null;
        entry.reason = 'Terminal attachment unavailable.'; entry.waiting = false;
      }
    }
  }

  private attached(): number { return [...this.entries.values()].filter(entry => entry.resource).length; }
  private eligible(entry: Entry<Slot>): boolean {
    return !!entry.resource && !this.visible(entry) && !this.pinned(entry);
  }
  private evict(entry: Entry<Slot>): void {
    const resource = entry.resource!;
    entry.savedState = resource.suspend?.(); entry.resource = null; entry.evictWhenUnpinned = false;
    entry.reason = 'Terminal detached to make room for another view.';
    resource.dispose();
  }
  private trimOverflow(): void {
    if (this.attached() <= this.maxAttached) {
      for (const entry of this.entries.values()) entry.evictWhenUnpinned = false;
      return;
    }
    for (const entry of this.entries.values()) {
      if (entry.evictWhenUnpinned && this.eligible(entry)) this.evict(entry);
    }
  }
  private makeRoom(): boolean {
    this.trimOverflow();
    const candidates = [...this.entries.values()].filter(entry => this.eligible(entry))
      .sort((a, b) => a.hiddenSince - b.hiddenSince);
    while (this.attached() >= this.maxAttached && candidates.length) this.evict(candidates.shift()!);
    if (this.attached() < this.maxAttached) return true;
    // A single overflow lets navigation proceed without discarding protected input.
    // Further views retain their slot request without creating another attachment.
    if (this.attached() >= this.maxAttached + 1) return false;
    const oldest = [...this.entries.values()].filter(entry => entry.resource)
      .sort((a, b) => a.hiddenSince - b.hiddenSince)[0];
    oldest.evictWhenUnpinned = true;
    return true;
  }

  dispose(key: string, reason: DisposeReason): void {
    const entry = this.entries.get(key);
    if (!entry) return;
    if (!['release', 'discard-input', 'permission-revoked', 'sign-out', 'shutdown'].includes(reason)) throw new Error('Disposal reason required.');
    if ((reason === 'release' && (entry.resource?.composing || Object.values(entry.pins).some(Boolean))) ||
        (reason === 'discard-input' && (entry.resource?.composing || entry.pins.dirty))) throw new Error('Resolve unsaved work or pending input before releasing this terminal.');
    this.entries.delete(key);
    for (const view of entry.views) this.views.delete(view);
    entry.resource?.dispose();
  }

  shutdown(): void {
    if (this.stopped) return;
    this.stopped = true;
    for (const key of [...this.entries.keys()]) this.dispose(key, 'shutdown');
    this.slots.clear();
  }

  private lookup(viewId: string): [View, Entry<Slot>] {
    const view = this.views.get(viewId);
    if (!view) throw new Error('Unknown terminal view.');
    return [view, this.entries.get(view.key)!];
  }
  private visible(entry: Entry<Slot>): boolean { return !!(entry.canonical && this.views.get(entry.canonical)?.visible); }
  private pinned(entry: Entry<Slot>): boolean { return !!entry.resource?.writerToken || !!entry.resource?.composing || Object.values(entry.pins).some(Boolean); }
  private checkVisible(entry: Entry<Slot>): void {
    if (!this.visible(entry) && [...this.entries.values()].filter(value => this.visible(value)).length >= this.maxVisible) throw new Error('Terminal visible limit reached. Hide or focus an existing terminal.');
  }
  private running(): void { if (this.stopped) throw new Error('View registry shutdown.'); }
}
