import { lazyImport } from '../lib/lazyImport';
import { createTerminalRuntime } from './terminalRuntime';
import type { BindTextInput } from './terminalRuntime';
import type { TerminalReference, TerminalResource, WriterPolicy } from '../lib/viewRegistry';
import '@xterm/xterm/css/xterm.css';

type Runtime = ReturnType<typeof createTerminalRuntime>;
type HostOptions = { fenced?: boolean; changed?: () => void; composition?: (reference: TerminalReference, composing: boolean) => void };
type RuntimeOptions = Parameters<typeof createTerminalRuntime>[0];

/** P2 stable host factory. Own one per browser tab; legacy terminals keep their own lifecycle. */
export class TerminalHostLayer {
  readonly element: HTMLDivElement;
  private entries = new Set<HostedTerminal>();
  private frame: number | undefined;
  private stopped = false;
  private observer: ResizeObserver;
  private observed = new Set<HTMLElement>();
  private refresh = () => {
    if (this.stopped || this.frame !== undefined) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = undefined;
      // Read every active slot before writing any host styles.
      const placements = [...this.entries].map(entry => [entry, entry.bounds()] as const);
      for (const [entry, rect] of placements) entry.position(rect);
    });
  };

  readonly options: HostOptions;
  readonly presentation: boolean;
  constructor(options: HostOptions | boolean = {}, readonly notify: () => void = () => {}, readonly authorityEnded: (reference: TerminalReference) => void = () => {}) {
    this.options = typeof options === 'boolean' ? {} : options;
    this.presentation = typeof options === 'boolean' && options;
    this.element = document.createElement('div');
    this.element.dataset.terminalHostLayer = '';
    Object.assign(this.element.style, { position: 'fixed', left: '0', top: '0', width: '0', height: '0', zIndex: '20', pointerEvents: 'none' });
    document.body.appendChild(this.element);
    this.observer = new ResizeObserver(this.refresh);
    this.element.addEventListener('compositionend', this.refresh, true);
    window.addEventListener('resize', this.refresh);
    window.addEventListener('scroll', this.refresh, true);
    window.visualViewport?.addEventListener('resize', this.refresh);
    window.visualViewport?.addEventListener('scroll', this.refresh);
  }

  create(reference: TerminalReference, policy: WriterPolicy): HostedTerminal {
    if (this.stopped) throw new Error('Terminal host layer disposed.');
    const entry = new HostedTerminal(this, reference, policy);
    this.entries.add(entry);
    return entry;
  }

  /** Called by a resource when its canonical slot changes; never reparent the host. */
  changed(_previous: HTMLElement | null, _next: HTMLElement | null) {
    if (this.stopped) return;
    const desired = new Set([...this.entries].map(entry => entry.observedSlot).filter((slot): slot is HTMLElement => !!slot));
    for (const slot of this.observed) if (!desired.has(slot)) this.observer.unobserve(slot);
    for (const slot of desired) if (!this.observed.has(slot)) this.observer.observe(slot);
    this.observed = desired;
    this.refresh();
  }
  remove(entry: HostedTerminal) { this.entries.delete(entry); this.changed(null, null); this.notify(); }

  dispose() {
    if (this.stopped) return;
    this.stopped = true;
    if (this.frame !== undefined) cancelAnimationFrame(this.frame);
    this.observer.disconnect();
    this.element.removeEventListener('compositionend', this.refresh, true);
    window.removeEventListener('resize', this.refresh);
    window.removeEventListener('scroll', this.refresh, true);
    window.visualViewport?.removeEventListener('resize', this.refresh);
    window.visualViewport?.removeEventListener('scroll', this.refresh);
    for (const entry of [...this.entries]) entry.dispose();
    this.element.remove();
  }
}

export class HostedTerminal implements TerminalResource<HTMLElement> {
  readonly host: HTMLDivElement;
  readonly ready: Promise<void>;
  private runtime?: Runtime;
  private layer: TerminalHostLayer;
  private slot: HTMLElement | null = null;
  private visible = false;
  private disposed = false;
  private failed = false;
  private closedReason: string | undefined;
  private reader = true;
  private shown = false;
  private reference: TerminalReference;
  private policy: WriterPolicy;

  constructor(layer: TerminalHostLayer, reference: TerminalReference, policy: WriterPolicy) {
    this.layer = layer; this.reference = reference; this.policy = policy;
    this.host = document.createElement('div');
    this.host.id = `terminal-host-${crypto.randomUUID()}`;
    Object.assign(this.host.style, { position: 'absolute', width: '640px', height: '360px', visibility: 'hidden', pointerEvents: 'auto', overflow: 'hidden' });
    this.host.inert = true;
    this.host.setAttribute('role', 'region');
    this.host.setAttribute('aria-label', 'Session terminal');
    layer.element.appendChild(this.host); // The only append for this host's lifetime.
    this.ready = layer.presentation ? Promise.resolve() : this.mount(reference, policy);
  }
  private async mount(reference: TerminalReference, policy: WriterPolicy) {
    try {
      let bind: BindTextInput | undefined;
      if (/^((?!chrome|chromium|android).)*safari/i.test(navigator.userAgent)) {
        bind = (await lazyImport(() => import('../lib/safariTerminalInput'))).bindSafariTextInput;
      }
      if (this.disposed) return;
      this.runtime = createTerminalRuntime({ host: this.host, sessionId: reference.sessionId,
        writerPolicy: policy, runId: this.layer.options.fenced || policy === 'p2-observer' ? reference.runId : undefined,
        instanceId: this.layer.options.fenced ? undefined : reference.installation,
        onAuthorityEnded: () => this.layer.authorityEnded(reference),
        onStatus: () => this.layer.options.changed?.(),
        onCompositionChange: composing => { this.layer.options.composition?.(reference, composing); this.layer.options.changed?.(); if (!composing) this.layer.changed(null, this.slot); },
        onClosed: (_reason, code) => {
          if (code && [4401, 4403, 4404, 4409].includes(code)) {
            this.closedReason = _reason; this.runtime?.dispose(); this.runtime = undefined; this.host.replaceChildren();
          }
          this.layer.options.changed?.();
        },
        autoFocus: false, preserveSelection: true, bindSafariTextInput: bind });
      this.runtime.xterm.textarea?.setAttribute('aria-label', 'Session terminal input');
      // No page-owned description element exists for these foundation entries.
      this.runtime.xterm.textarea?.removeAttribute('aria-describedby');
      this.runtime.setVisibility(false);
      this.reader = this.runtime.xterm.options.screenReaderMode ?? true;
      this.setVisibility(this.visible);
      this.restoreScroll();
    } catch {
      this.failed = true;
      this.runtime?.dispose();
      this.host.replaceChildren();
      // Keep a neutral placeholder; never expose exception paths or tokens.
      this.host.textContent = 'Terminal attachment unavailable.';
      this.layer.options.changed?.();
    }
  }
  /** Presentation subscribes; this handle owns construction and final cleanup. */
  attach(options: RuntimeOptions): Runtime {
    if (this.disposed) throw new Error('Terminal attachment released.');
    if (!this.runtime) {
      this.runtime = createTerminalRuntime({ ...options,
        sessionId: this.reference.sessionId, writerPolicy: this.policy, instanceId: this.reference.installation,
        runId: this.policy === 'p2-observer' ? this.reference.runId : undefined,
        autoFocus: false, preserveSelection: true, onAuthorityEnded: () => this.layer.authorityEnded(this.reference) });
      this.reader = this.runtime.xterm.options.screenReaderMode ?? true;
    }
    this.runtime.setVisibility(this.visible);
    return this.runtime;
  }
  setScreenReader(enabled: boolean) {
    this.reader = enabled;
    this.applyScreenReader(this.visible && this.shown && enabled);
  }
  private applyScreenReader(enabled: boolean) {
    if (!this.runtime) return;
    this.runtime.xterm.options.screenReaderMode = enabled;
    // Visibility can recreate xterm's accessibility tree after the page effect ran.
    this.host.querySelector('.live-region')?.setAttribute('aria-live', 'off');
  }
  get isVisible() { return this.visible; }
  get isDisposed() { return this.disposed; }
  get observedSlot() { return !this.disposed && this.visible ? this.slot : null; }
  get terminal() { return this.runtime?.xterm; }
  get socket() { return this.runtime?.socket; }
  get status() { return this.runtime?.status ?? { kind: this.failed || this.closedReason ? 'closed' : 'connecting', reason: this.closedReason ?? 'Terminal attachment unavailable.' } as const; }
  get pendingInput() { return this.runtime?.pendingInput ?? false; }
  requestControl(take = false, reason = '', confirmPartial = false) { this.runtime?.requestControl(take, reason, confirmPartial); }
  releaseControl(confirmPartial = false) { this.runtime?.releaseControl(confirmPartial); }
  get partialInput() { return this.runtime?.partialInput ?? false; }
  get busyInput() { return this.runtime?.busyInput ?? false; }
  refreshControl() { this.runtime?.refreshControl(); }
  setInputTransform(value?: (data: string) => string) { this.runtime?.setInputTransform(value); }
  send(data: string) { this.runtime?.send(data); }
  /** A local acknowledged attachment token, not a new server authority grant. */
  get writerToken() { return this.runtime?.writerToken ?? null; }
  get composing() { return this.runtime?.composing ?? false; }
  get state(): 'connecting' | 'open' | 'closed' { return this.failed || this.closedReason || this.disposed ? 'closed' : this.runtime?.status.kind ?? 'connecting'; }

  place(slot: HTMLElement | null) {
    if (this.disposed || slot === this.slot) return;
    const previous = this.slot; this.slot = slot;
    this.layer.changed(previous, slot);
    if (!slot) this.position(null);
  }
  setVisibility(visible: boolean) {
    if (this.disposed) return;
    const changed = this.visible !== visible;
    this.visible = visible;
    if (changed) this.layer.notify();
    this.layer.changed(null, this.slot);
    if (!visible) this.position(null);
  }
  bounds(): DOMRect | null {
    if (!this.visible || !this.slot?.isConnected || !this.slot.getClientRects().length) return null;
    const rect = this.slot.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 ? rect : null;
  }
  position(rect: DOMRect | null) {
    if (this.disposed) return;
    if (this.composing && this.shown) return;
    if (!rect) {
      this.host.style.visibility = 'hidden'; this.host.inert = true;
      if (this.runtime) {
        if (this.shown && !this.layer.presentation) this.reader = this.runtime.xterm.options.screenReaderMode ?? this.reader;
        this.applyScreenReader(false);
        this.runtime.setVisibility(false);
      }
      this.shown = false;
      return;
    }
    const wasShown = this.shown;
    this.shown = true;
    Object.assign(this.host.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`, visibility: 'visible' });
    // Runtime-only P2 hosts follow scroll clips. Presentation hosts also own
    // fixed dialogs/expanded chrome, which must retain their reviewed overlays.
    if (!this.layer.presentation) {
      // A stable body host must still obey its slot's scrolling ancestors.
      let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
      let left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
      for (let parent = this.slot?.parentElement; parent; parent = parent.parentElement) {
        // A fixed expanded panel escapes the retained column's scroll clip.
        if (parent.matches('[data-registered-terminal][aria-modal="true"]')) break;
        const style = getComputedStyle(parent);
        if (/(auto|scroll|hidden|clip)/.test(style.overflow + style.overflowX + style.overflowY)) {
          const box = parent.getBoundingClientRect(); top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom); left = Math.max(left, box.left); right = Math.min(right, box.right);
        }
      }
      this.host.style.clipPath = `inset(${Math.max(0, top - rect.top)}px ${Math.max(0, rect.right - right)}px ${Math.max(0, rect.bottom - bottom)}px ${Math.max(0, left - rect.left)}px)`;
    }
    this.host.inert = false;
    if (this.runtime) {
      if (!wasShown) this.applyScreenReader(this.reader);
      this.runtime.setVisibility(true);
    }
  }
  private restoredScroll?: number;
  suspend() {
    const buffer = this.terminal?.buffer.active;
    this.releaseControl();
    // A reader at the bottom should continue following output after reattach.
    return buffer && buffer.viewportY < buffer.baseY ? buffer.viewportY : undefined;
  }
  restore(state: unknown) {
    if (typeof state === 'number') { this.restoredScroll = state; this.restoreScroll(); }
  }
  private restoreScroll() {
    if (this.restoredScroll === undefined || !this.runtime) return;
    const term = this.runtime.xterm, line = this.restoredScroll;
    this.restoredScroll = undefined;
    const restored = term.onWriteParsed(() => {
      term.scrollToLine(line);
      if (term.buffer.active.baseY >= line) restored.dispose();
    });
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.layer.changed(this.slot, null); this.slot = null;
    this.runtime?.dispose(); this.runtime = undefined; this.host.remove(); this.layer.remove(this);
  }
}
