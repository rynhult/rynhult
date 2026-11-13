import { TerminalInputState } from '../lib/terminalInputState';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import type { WriterPolicy } from '../lib/viewRegistry';
export type BindTextInput = typeof import('../lib/safariTerminalInput').bindSafariTextInput;
export type TerminalStatus = { kind: 'connecting' } | { kind: 'open'; write: boolean; held?: boolean; partial?: boolean; reclaim?: boolean; reason?: string } | { kind: 'closed'; reason: string };
export type TerminalSelection = { text: string; x: number; y: number } | null;

/** Owns one editor/attachment. Legacy Terminal retains its existing mount policy. */
export function createTerminalRuntime({ host, sessionId, touch = false, compact = false, expanded = false,
  screenReader = true, take = false, takeReason = '', writerPolicy = 'p1-claim-if-free', autoFocus = true,
  preserveSelection = false, runId, instanceId, onAuthorityEnded, onCompositionChange, bindSafariTextInput, onStatus = () => {}, onSelection = () => {}, onClosed, transformInput,
}: {
  host: HTMLElement; sessionId: string; touch?: boolean; compact?: boolean; expanded?: boolean;
  screenReader?: boolean; take?: boolean; takeReason?: string; writerPolicy?: WriterPolicy; autoFocus?: boolean;
  preserveSelection?: boolean; runId?: string; instanceId?: string; onAuthorityEnded?: () => void; onCompositionChange?: (composing: boolean) => void; bindSafariTextInput?: BindTextInput;
  onStatus?: (status: TerminalStatus) => void; onSelection?: (selection: TerminalSelection) => void;
  onClosed?: (reason: string, code?: number) => void;
  transformInput?: (data: string) => string;
}) {
  let currentStatus: TerminalStatus = { kind: 'connecting' };
  let visible = true, composing = false, disposed = false;
  const observerRun = !!runId && !!instanceId;
  const fencedRun = !!runId && !instanceId;
  const updateStatus = (status: TerminalStatus) => { currentStatus = status; onStatus(status); if (writerPolicy === 'p2-observer') xterm.options.disableStdin = !visible || status.kind !== 'open' || !status.write; };
  const xterm = new XTerm({
    fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace',
    fontSize: touch || compact ? 12 : 13,
    lineHeight: 1.15,
    cursorBlink: true,
    scrollback: 10000,
    allowProposedApi: true,
    macOptionClickForcesSelection: true,
    screenReaderMode: (!compact || expanded) && screenReader,
    theme: { background: "#0b0d10", foreground: "#e6e8eb", cursor: "#7aa2ff", selectionBackground: "#52627a" },
  });
  let ownedSocket: WebSocket | undefined;
  let disposeTextInput: (() => void) | undefined;
  let disposeComposition = () => {};
  try {
    const fit = new FitAddon();
    xterm.loadAddon(fit);
    xterm.loadAddon(new WebLinksAddon((_, uri) => window.open(uri, "_blank", "noopener")));
    xterm.open(host);
    for (const [name, value] of Object.entries({ autocomplete: 'off', autocorrect: 'off', spellcheck: 'false',
      autocapitalize: 'off', writingsuggestions: 'false' })) xterm.textarea?.setAttribute(name, value);
    // Observe before Safari's host capture adapter stops propagation. A
    // textarea/bubble listener would miss composition and permit idle eviction.
    const compositionStart = (event: CompositionEvent) => { if (event.target === xterm.textarea) { composing = true; sendPins(); onCompositionChange?.(true); } };
    const compositionEnd = (event: CompositionEvent) => {
      if (event.target !== xterm.textarea) return;
      composing = false; sendPins(); onCompositionChange?.(false);
      if (preserveSelection) requestResize();
    };
    host.addEventListener('compositionstart', compositionStart, true);
    host.addEventListener('compositionend', compositionEnd, true);
    disposeComposition = () => {
      host.removeEventListener('compositionstart', compositionStart, true);
      host.removeEventListener('compositionend', compositionEnd, true);
    };
    disposeTextInput = bindSafariTextInput?.(xterm, host);
    if (!compact || expanded) xterm.textarea?.setAttribute("aria-describedby", `${expanded ? "expanded-leave" : "leave"}-${sessionId}`);
    // Screen readers read the rows on demand; xterm's own assertive region would read the agent's stream aloud as it
    // arrives. State changes are announced once through Rynhult's Announcer instead.
    host.querySelector(".live-region")?.setAttribute("aria-live", "off");
    fit.fit();
    const selected = xterm.onSelectionChange(() => {
      const text = xterm.getSelection().split(/\r?\n/).map(line => line.trimEnd()).join('\n');
      const bounds = host?.getBoundingClientRect();
      const end = xterm.getSelectionPosition()?.end;
      onSelection(text && bounds ? {text, x:bounds.left + (end?.x ?? 0) * bounds.width / xterm.cols,
        y:bounds.top + Math.max(0, Math.min(xterm.rows - 1, (end?.y ?? 0) - xterm.buffer.active.viewportY)) * bounds.height / xterm.rows} : null);
    });

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const params = new URLSearchParams({ mode: writerPolicy === "p1-claim-if-free" ? "write" : "read", rows: String(xterm.rows), cols: String(xterm.cols), ...(runId ? instanceId ? { run: runId, instance: instanceId } : { run_id: runId } : {}), ...(take && writerPolicy === "p1-claim-if-free" ? { take: "1", take_reason: takeReason, confirm_partial: "true" } : {}) });
    params.set('upload_id', crypto.randomUUID());
    const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${sessionId}/tty?${params}`);
    ownedSocket = ws;
    ws.binaryType = "arraybuffer";
    updateStatus({ kind: "connecting" });

    let wheelHistory = false, wheelRemainder = 0;
    let epoch = -1; let pendingInput = 0; const inputState = new TerminalInputState();
    const frameIdentity = () => fencedRun ? { run_id: runId, epoch } : {};
    const sendPins = () => { if (!observerRun && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'pins', busy: composing || pendingInput > 0, partial: inputState.partial, ...frameIdentity() })); };
    const writable = () => currentStatus.kind === 'open' && currentStatus.write;
    const send = (data: string) => {
      if (!visible || !writable() || ws.readyState !== WebSocket.OPEN) return;
      inputState.push(data);
      if (currentStatus.kind === 'open' && currentStatus.partial && !inputState.partial) updateStatus({ ...currentStatus, partial: false, reason: undefined });
      pendingInput++; sendPins();
      ws.send(JSON.stringify({ t: 'i', d: data, ...frameIdentity() }));
    };
    xterm.attachCustomWheelEventHandler(event => {
      if (!wheelHistory || !visible || !writable() || ws.readyState !== WebSocket.OPEN) return true;
      // A 120px wheel notch is three rows; retain fractional trackpad movement.
      const delta = event.deltaY * (event.deltaMode === 1 ? 1 : event.deltaMode === 2 ? xterm.rows : 1 / 40);
      if (!Number.isFinite(delta)) return false;
      if (Math.sign(delta) !== Math.sign(wheelRemainder)) wheelRemainder = 0;
      wheelRemainder += delta;
      const lines = Math.max(-200, Math.min(200, Math.trunc(wheelRemainder + Math.sign(wheelRemainder) * 1e-9)));
      if (lines) {
        wheelRemainder -= lines;
        ws.send(JSON.stringify({ t: 'wheel', lines, ...frameIdentity() }));
      }
      return false;
    });
    let sentSize = '';
    let observerGrid: {rows: number; cols: number} | undefined;
    const fixObserverGrid = (rows: number, cols: number) => {
      if (!Number.isInteger(rows) || rows < 5 || rows > 500 || !Number.isInteger(cols) || cols < 20 || cols > 1000) return;
      observerGrid = {rows, cols};
      if (xterm.cols !== cols || xterm.rows !== rows) xterm.resize(cols, rows);
      const screen = host.querySelector<HTMLElement>('.xterm-screen');
      if (xterm.element && screen) {
        xterm.element.style.width = screen.style.width;
        xterm.element.style.height = screen.style.height;
      }
      host.style.overflow = 'auto';
    };
    const resize = () => {
      if (observerGrid) { fixObserverGrid(observerGrid.rows, observerGrid.cols); return; }
      if (preserveSelection && composing) return;
      const dims = fit.proposeDimensions();
      if (!dims || !Number.isFinite(dims.rows) || !Number.isFinite(dims.cols)) return;
      if (dims.rows !== xterm.rows || dims.cols !== xterm.cols) {
        const selection = preserveSelection ? xterm.getSelectionPosition() : undefined;
        const text = xterm.getSelection();
        const buffer = xterm.buffer.active;
        const selectionAnchor = selection ? xterm.registerMarker(selection.start.y - buffer.baseY - buffer.cursorY) : undefined;
        const reading = preserveSelection && buffer.viewportY < buffer.baseY
          ? xterm.registerMarker(buffer.viewportY - buffer.baseY - buffer.cursorY) : undefined;
        fit.fit();
        if (reading && !reading.isDisposed) xterm.scrollToLine(reading.line);
        reading?.dispose();
        if (selection && text) {
          // Locate the exact selected content after reflow. Map UTF-16 offsets to
          // terminal cells (wide/combined characters are not one cell per char).
          // Use line strings to find candidates, then map only the matching rows;
          // retaining a full cell map for 10,000 scrollback rows is unnecessary.
          let content = '';
          const rows: { offset: number; y: number }[] = [];
          for (let y = 0; y < buffer.length; y++) {
            const line = buffer.getLine(y)!;
            if (y && !line.isWrapped) content += '\n';
            const row = line.translateToString(true).replace(/\u00a0/g, ' ');
            rows.push({ offset: content.length, y }); content += row;
          }
          const rowAt = (offset: number, end = false) => {
            let low = 0, high = rows.length;
            while (low < high) { const mid = (low + high) >>> 1; if (end ? rows[mid].offset < offset : rows[mid].offset <= offset) low = mid + 1; else high = mid; }
            return rows[Math.max(0, low - 1)];
          };
          const needle = text.replace(/\r\n/g, '\n');
          const anchorY = selectionAnchor && !selectionAnchor.isDisposed ? selectionAnchor.line : selection.start.y;
          let found = content.indexOf(needle), chosen = -1, distance = Infinity;
          while (found >= 0) {
            const row = rowAt(found);
            if (Math.abs(row.y - anchorY) < distance) { chosen = found; distance = Math.abs(row.y - anchorY); }
            found = content.indexOf(needle, found + 1);
          }
          const point = (offset: number, end: boolean) => {
            const row = rowAt(offset, end);
            const line = buffer.getLine(row.y)!; let chars = row.offset;
            for (let x = 0; x < xterm.cols; x++) {
              const cell = line.getCell(x)!; if (!cell.getWidth()) continue;
              if (chars >= offset) return { x, y: row.y };
              chars += (cell.getChars() || ' ').length;
              if (end && chars >= offset) return { x: x + cell.getWidth(), y: row.y };
            }
            return { x: xterm.cols, y: row.y };
          };
          if (chosen >= 0) {
            const start = point(chosen, false), end = point(chosen + needle.length, true);
            xterm.select(start.x, start.y, (end.y - start.y) * xterm.cols + end.x - start.x);
            if (xterm.getSelection() !== text) xterm.clearSelection(); // Never select unrelated content.
          }
        }
        selectionAnchor?.dispose();
      }
      const size = `${xterm.rows}:${xterm.cols}`;
      // ResizeObserver may notify after surrounding status/layout updates even
      // when character dimensions did not change. Keep the PTY/provider out
      // of surrounding status updates that do not resize the actual editor.
      if (visible && writable() && ws.readyState === WebSocket.OPEN && size !== sentSize) {
        ws.send(JSON.stringify({ t: "r", rows: xterm.rows, cols: xterm.cols, ...frameIdentity() }));
        sentSize = size;
      }
    };
    let resizeFrame: number | undefined;
    const requestResize = () => {
      if (!preserveSelection) { resize(); return; }
      if (resizeFrame !== undefined || disposed) return;
      resizeFrame = requestAnimationFrame(() => { resizeFrame = undefined; if (!disposed) resize(); });
    };
    let verified = !observerRun;
    ws.onopen = requestResize;
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        const message = JSON.parse(e.data);
        if (message.t === 'mode' && observerRun) {
          if (message.protocol !== 'run-observer-v1' || message.run !== runId || message.instance !== instanceId || message.write !== false
              || !Number.isInteger(message.rows) || message.rows < 5 || message.rows > 500
              || !Number.isInteger(message.cols) || message.cols < 20 || message.cols > 1000) {
            updateStatus({ kind: 'closed', reason: 'Run-bound terminal unavailable. Reload Rynhult to retry.' });
            ws.close(); return;
          }
          verified = true;
        }
        if (message.t === 'geometry' && !writable()) fixObserverGrid(message.rows, message.cols);
        if (!observerRun) {
        if (message.t === 'mode') {
          wheelHistory = message.wheel === true; wheelRemainder = 0;
          if (runId && (message.run_id !== runId || !Number.isSafeInteger(message.epoch))) return;
          if (runId) epoch = message.epoch;
          if (message.write) {
            if (observerGrid) sentSize = '';
            observerGrid = undefined;
            xterm.element?.style.removeProperty('width');
            xterm.element?.style.removeProperty('height');
            host.style.removeProperty('overflow');
          } else fixObserverGrid(message.rows, message.cols);
          if (message.write && typeof message.partial === 'boolean') inputState.partial = message.partial;
          updateStatus({ kind: 'open', write: !!message.write && (writerPolicy === 'p1-claim-if-free' || !!runId), held: message.held, partial: message.partial, reclaim: message.reclaim === true, reason: message.reason });
          requestResize();
        }
        if (message.t === 'ack') { pendingInput = Math.max(0, pendingInput - 1); sendPins(); onStatus(currentStatus); }
        } else if (message.t === 'mode') {
          fixObserverGrid(message.rows, message.cols);
          updateStatus({ kind: 'open', write: false });
        }
        if (message.t === "closed") updateStatus({ kind: "closed", reason: message.reason });
        return;
      }
      if (verified) xterm.write(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      // A disconnected observer cannot establish continuing access. Clear it
      // on EVERY close, including network/restart codes and pre-accept 1006.
      // Explicit Recheck resolves fresh authority before recreating it.
      if (observerRun) {
        onAuthorityEnded?.();
        if (disposed) return;
        xterm.reset();
      }
      const reasons: Record<number, string> = {
        4001: "Another window took control of this terminal.",
        4401: "Your login expired.",
        4403: "Terminal access denied.",
        4404: "This session has no terminal right now.",
        4409: "Terminal access or this run ended. Recheck the session.",
      };
      const reason = reasons[e.code] ?? "Disconnected.";
      currentStatus = currentStatus.kind === 'closed' ? currentStatus : { kind: 'closed', reason };
      if (onClosed) onClosed(reason, e.code); else onStatus(currentStatus);
    };
    const input = xterm.onData((data) => {
      send(transformInput?.(data) ?? data);
    });
    const observer = new ResizeObserver(requestResize);
    observer.observe(host);
    window.addEventListener('resize', requestResize);
    // Character measurement can settle after the host has reached its final
    // size (for example when Chromium reveals a retained terminal). A host-only
    // observer misses that change and leaves the initial row count in place.
    // Screen dimensions also change after fit; resize() deduplicates both the
    // editor geometry and PTY messages, so the following notification is a no-op.
    const screen = host.querySelector('.xterm-screen');
    if (screen) observer.observe(screen);
    // A late lazy mount must not take focus back from a tab or another control.
    if (autoFocus && !compact && !touch && document.activeElement === document.body) xterm.focus();
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      if (resizeFrame !== undefined) cancelAnimationFrame(resizeFrame);
      disposeComposition();
      observer.disconnect();
      window.removeEventListener('resize', requestResize);
      input.dispose();
      selected.dispose();
      disposeTextInput?.();
      ws.close();
      xterm.dispose();
    };

    return { xterm, socket: ws, resize, dispose, send,
      setInputTransform(value?: (data: string) => string) { transformInput = value; },
      requestControl(take = false, reason = '', confirmPartial = false) { if (fencedRun && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'control', ...frameIdentity(), take, reason, confirm_partial: confirmPartial })); },
      releaseControl(confirmPartial = false) { if (fencedRun && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'release', ...frameIdentity(), confirm_partial: confirmPartial })); },
      refreshControl() { if (fencedRun && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'status' })); },
      get pendingInput() { return pendingInput > 0 || inputState.partial; },
      get partialInput() { return inputState.partial; },
      get busyInput() { return pendingInput > 0 || composing; },
      get composing() { return composing; },
      get status() { return currentStatus; },
      get writerToken() { return !disposed && currentStatus.kind === 'open' && currentStatus.write ? ws : null; },
      setVisibility(value: boolean) {
        visible = value;
        xterm.options.disableStdin = !visible || writerPolicy === 'p2-observer' && !writable();
        if (xterm.textarea) xterm.textarea.tabIndex = value ? 0 : -1;
        if (value) requestResize();
      },
    };
  } catch (error) {
    disposeComposition();
    disposeTextInput?.();
    ownedSocket?.close();
    xterm.dispose();
    throw error;
  }
}
