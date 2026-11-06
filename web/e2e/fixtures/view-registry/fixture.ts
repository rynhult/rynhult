import { ViewRegistry, type TerminalReference } from '../../../src/lib/viewRegistry';
import { TerminalHostLayer, type HostedTerminal } from '../../../src/components/terminalHostLayer';
const layer = new TerminalHostLayer();
const resources: HostedTerminal[] = [];
const registry = new ViewRegistry<HTMLElement>({
  writerPolicy: new URL(location.href).searchParams.has('p1') ? 'p1-claim-if-free' : 'p2-observer',
  create(reference, policy) { const terminal = layer.create(reference, policy); resources.push(terminal); return terminal; },
});
const reference: TerminalReference = { installation: 'test', person: 'person', sessionId: 'fake-session', runId: 'fake-run', node: 'fake-node', account: 'fake-account' };
const removers = ['one', 'two', 'fill'].map(id => registry.registerSlot(id, document.getElementById(id)!));
const first = registry.acquire(reference);
registry.place(first.viewId, 'one');
if (new URL(location.href).searchParams.has('disposeEarly')) { registry.shutdown(); layer.dispose(); }
const initialHost = resources[0].host, initialParent = initialHost.parentNode;
await resources[0].ready;
const initialTerm = resources[0].terminal, initialSocket = resources[0].socket;
const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
const api = {
  registry, resources, first,
  async move(slot: string) { registry.place(first.viewId, slot); await frames(); },
  async hide() { registry.setVisibility(first.viewId, false); await frames(); },
  async show() { registry.setVisibility(first.viewId, true); await frames(); },
  async unregister() { removers[0](); await frames(); },
  async second() { return registry.acquire(reference); },
  snapshot() {
    const resource = resources[0], term = resource.terminal!;
    const history = Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i)!.translateToString(true));
    return { inputSubscribers: (term as any)._core._onData._size, sameTerm: term === initialTerm, sameSocket: resource.socket === initialSocket,
      sameHost: resource.host === initialHost, sameParent: resource.host.parentNode === initialParent,
      composing: resource.composing, selection: term.getSelection(), history, writer: !!resource.writerToken,
      state: registry.inspect(first.resourceKey), focused: document.activeElement === term.textarea,
      inert: resource.host.inert, stdinDisabled: term.options.disableStdin, count: resources.length };
  },
  shutdown() { registry.shutdown(); layer.dispose(); },
};
(window as unknown as { fixture: typeof api }).fixture = api;
window.addEventListener('pagehide', () => api.shutdown(), { once: true });
