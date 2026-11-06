import { terminalKey, ViewRegistry, type TerminalReference } from '../lib/viewRegistry';
import { TerminalHostLayer, type HostedTerminal } from './terminalHostLayer';

export class TerminalOwner {
  private layer?: TerminalHostLayer;
  private registry?: ViewRegistry<HTMLElement>;
  private resources = new Map<string, HostedTerminal>();
  private handles = new Map<string, { viewId: string; resourceKey: string; reference: TerminalReference }>();
  private listeners = new Set<() => void>();
  private revision = 0;
  private claimedIntents = new Map<string, string>();
  consumeIntent(key: string, intent: string) {
    if (this.claimedIntents.get(key) === intent) return false;
    this.claimedIntents.set(key, intent); return true;
  }
  composition: (reference: TerminalReference, composing: boolean) => void = () => {};
  subscribe = (callback: () => void) => { this.listeners.add(callback); return () => { this.listeners.delete(callback); }; };
  snapshot = () => this.revision;
  private emit = () => { this.revision++; this.listeners.forEach(callback => callback()); };
  acquire(key: string, reference: TerminalReference, slot: HTMLElement) {
    this.syncPins();
    if (!this.layer) {
      this.layer = new TerminalHostLayer({ fenced: true, changed: this.emit, composition: (ref, composing) => this.composition(ref, composing) });
      this.registry = new ViewRegistry({ create: (ref, policy) => {
        const resource = this.layer!.create(ref, policy); this.resources.set(terminalKey(ref), resource); return resource;
      } });
    }
    let handle = this.handles.get(key);
    if (handle && handle.resourceKey !== terminalKey(reference)) this.release(key);
    handle = this.handles.get(key);
    if (!handle) { handle = { ...this.registry!.acquire(reference), reference }; this.handles.set(key, handle); }
    const unregister = this.registry!.registerSlot(key, slot);
    this.registry!.place(handle.viewId, key); this.emit();
    return () => { unregister(); };
  }
  resource(key: string) { const handle = this.handles.get(key); const resource = handle ? this.resources.get(handle.resourceKey) : undefined; return resource?.isDisposed ? undefined : resource; }
  inspect(key: string) { const handle = this.handles.get(key); return handle ? this.registry?.inspect(handle.resourceKey) : undefined; }
  setVisibility(key: string, visible: boolean) {
    const handle = this.handles.get(key); if (!handle || !this.registry?.inspect(handle.resourceKey)) return;
    this.registry.setVisibility(handle.viewId, visible); this.layer?.changed(null, null);
  }
  private syncPins() {
    for (const [key, handle] of this.handles) if (this.registry?.inspect(handle.resourceKey)) {
      if (this.registry.inspect(handle.resourceKey)?.state === 'detached') this.resources.delete(handle.resourceKey);
      const resource = this.resource(key);
      this.registry.setPins(handle.resourceKey, { unsent: !!resource?.pendingInput, uncertain: !!resource?.pendingInput && resource.state === 'closed' });
    }
  }
  hasPendingInput() { return [...this.resources.values()].some(resource => resource.pendingInput || resource.composing); }
  release(key: string, revoked = false, discardInput = false) {
    this.syncPins();
    const handle = this.handles.get(key); if (!handle) return;
    this.registry?.dispose(handle.resourceKey, revoked ? 'permission-revoked' : discardInput ? 'discard-input' : 'release');
    this.resources.delete(handle.resourceKey); this.handles.delete(key); this.emit();
  }
  revokeSession(sessionId: string) { for (const [key, handle] of this.handles) if (handle.reference.sessionId === sessionId) this.release(key, true); }
  hiddenPartial(except: string) { return [...this.handles.keys()].filter(key => key !== except && !this.inspect(key)?.visible && this.resource(key)?.partialInput); }
  retainedWriters() { return [...this.handles.keys()].filter(key => this.resource(key)?.writerToken); }
  sweep() { this.syncPins(); this.registry?.sweep(); this.syncPins(); this.emit(); }
  refreshGeometry() { this.layer?.changed(null, null); }
  shutdown() { this.registry?.shutdown(); this.layer?.dispose(); this.registry = undefined; this.layer = undefined; this.handles.clear(); this.resources.clear(); }
}
