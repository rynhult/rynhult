import prefixList from './sessionRelayPrefixes.json' with { type: 'json' };

// Python text_prefixes.json is authoritative; the parity test pins this build-local copy.
const refusedPrefixes: readonly string[] = prefixList;
/** One immutable browser input generation. Nothing is replayed after retirement or uncertainty. */
export interface InputWatermark { epoch: number; admitted: number; processed: number }
export interface InputTransport {
  claim(): Promise<{ inputGrant: string; inputEpoch: number }>;
  write(grant: string, sequence: number, data: string): Promise<{ watermark: InputWatermark; partial: boolean }>;
  drain(grant: string): Promise<InputWatermark>;
  idle(): Promise<boolean>;
}
export interface InputState {
  phase: 'new' | 'claiming' | 'active' | 'uncertain' | 'retired';
  draft: string; pending: number; waiting: boolean; uncertain: boolean; status: string;
}
export class RelayInputGeneration {
  readonly epoch: number; readonly transport: InputTransport; readonly changed: () => void;
  state: InputState = { phase: 'new', draft: '', pending: 0, waiting: false, uncertain: false, status: 'Input is disconnected.' };
  private grant?: string; private sequence = 0; private processed = 0;
  private queue: string[] = []; private writing = false; private reading = false; private submitted?: number;
  private readonly beforeEnter: () => Promise<void>;
  private readonly nextFrame: () => Promise<void>;
  constructor(epoch: number, transport: InputTransport, changed: () => void, beforeEnter = async () => {},
    nextFrame = () => new Promise<void>(resolve => {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => resolve());
      else queueMicrotask(resolve);
    })) {
    this.epoch = epoch; this.transport = transport; this.changed = changed; this.beforeEnter = beforeEnter; this.nextFrame = nextFrame;
  }
  private notify() { this.state.pending = this.queue.length + (this.writing ? 1 : 0); this.changed(); }
  private freeze(message: string) {
    if (this.state.phase === 'retired') return;
    this.state.phase = 'uncertain'; this.state.uncertain = true; this.state.status = message; this.queue = []; this.notify();
  }
  retire(message = 'Previous input is permanently read-only. Nothing will be replayed.') {
    const unresolved = this.state.phase === 'uncertain';
    this.state.phase = 'retired'; this.state.status = unresolved ? message + ' Prior delivery remains unresolved.' : message; this.queue = []; this.notify();
  }
  async connect() {
    if (this.state.phase !== 'new') return;
    this.state.phase = 'claiming'; this.state.status = 'Connecting input…'; this.notify();
    try {
      const result = await this.transport.claim();
      if (this.state.phase !== 'claiming') return;
      if (typeof result.inputGrant !== 'string' || !result.inputGrant || result.inputEpoch !== this.epoch) throw new Error('Input grant belongs to another generation');
      this.grant = result.inputGrant; this.state.phase = 'active'; this.state.status = 'Keyboard connected.'; this.notify();
    } catch { this.freeze('Input connection is unresolved. Take input again when safe; nothing will be replayed.'); }
  }
  enqueue(data: string) {
    if (this.state.phase !== 'active' || this.state.waiting) return false;
    if (data.length !== 1 || !/^[\x20-\x7e\x7f\r]$/.test(data)) throw new Error('This keyboard control is unavailable.');
    if (data === '\x7f' && !this.state.draft) throw new Error('The native draft is empty.');
    const draft = data === '\x7f' ? this.state.draft.slice(0, -1) : data === '\r' ? this.state.draft : this.state.draft + data;
    if ((data === '\r' && !draft.trim()) || refusedPrefixes.some(prefix => draft.trimStart().startsWith(prefix))) throw new Error('This native command is unavailable.');
    if (draft.length > 4096 || this.queue.length >= 512) throw new Error('Input is full. Wait for pending typing before adding more.');
    this.state.draft = draft;
    if (data === '\r') { this.state.waiting = true; this.state.status = 'Waiting for native processing…'; }
    this.queue.push(data); this.notify(); void this.pump(); return true;
  }
  private watermark(value: InputWatermark, admitted: number) {
    if (value.epoch !== this.epoch || !Number.isSafeInteger(value.admitted) || value.admitted !== admitted
      || !Number.isSafeInteger(value.processed) || value.processed < this.processed || value.processed > admitted) throw new Error('Unconfirmed input watermark');
    this.processed = value.processed;
  }
  private async pump() {
    if (this.writing || this.state.phase !== 'active' || !this.grant) return;
    this.writing = true;
    try {
      while (this.queue.length && this.state.phase === 'active') {
        // Batch only queued supported keys, never across Enter. There is one
        // outstanding frame: uncertainty/retirement drops all unsent bytes.
        if (this.queue[0] !== '\r') {
          // A rAF can run immediately after the first key. Give a burst one
          // full frame to coalesce, bounded to three callbacks. Enter seals the
          // prefix and flushes at the next callback without another delay.
          let buffered = this.queue.length;
          for (let frame = 0; frame < 3; frame++) {
            await this.nextFrame();
            if (this.state.phase !== 'active') return;
            if (this.queue.at(-1) === '\r' || frame > 0 && this.queue.length === buffered) break;
            buffered = this.queue.length;
          }
        }
        if (this.state.phase !== 'active') return;
        let data = this.queue.shift()!;
        if (data === '\r') {
          await this.beforeEnter();
          if (this.state.phase !== 'active') return;
        } else {
          while (this.queue.length && this.queue[0] !== '\r') data += this.queue.shift()!;
        }
        const sequence = ++this.sequence;
        if (data === '\r') this.submitted = sequence;
        this.notify();
        const result = await this.transport.write(this.grant, sequence, data);
        if (this.state.phase !== 'active') return;
        if (result.partial !== false) throw new Error('Partial input');
        this.watermark(result.watermark, sequence);
      }
    } catch { this.freeze('Input delivery is unknown. Review the native session; nothing will be replayed.'); }
    finally { this.writing = false; this.notify(); }
  }
  async refresh() {
    if (this.reading || this.writing || this.queue.length || this.state.phase !== 'active' || !this.grant || !this.state.waiting) return;
    this.reading = true;
    try {
      if (this.submitted !== undefined) {
        const value = await this.transport.drain(this.grant);
        if (this.state.phase !== 'active') return;
        this.watermark(value, this.sequence);
        if (this.processed < this.submitted) return;
        this.submitted = undefined; this.state.draft = ''; this.state.status = 'Waiting for the agent to be idle…'; this.notify();
      }
      // Start a fresh read after processing; an older panel snapshot proves no idle boundary.
      const idle = await this.transport.idle();
      if (this.state.phase === 'active' && idle) { this.state.waiting = false; this.state.status = 'Keyboard connected.'; this.notify(); }
    } catch { this.freeze('Native processing cannot be confirmed. Nothing will be replayed.'); }
    finally { this.reading = false; }
  }
}
