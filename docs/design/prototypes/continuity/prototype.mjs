import { Terminal } from '/xterm.mjs';
import { FitAddon } from '/fit.mjs';

const approach = new URL(location.href).searchParams.get('approach') || 'stable';
const guarded = new URL(location.href).searchParams.get('guarded') !== '0';
const host = document.createElement('div');
host.id = 'host';
const layer = document.querySelector('#layer');
const firstSlot = document.querySelector('#column1');
(approach === 'stable' ? layer : firstSlot).appendChild(host);
const parent = host.parentNode;
const term = new Terminal({ scrollback: 10000, fontSize: 14, allowProposedApi: false });
const fit = new FitAddon();
term.loadAddon(fit);
const measurements = {
  moves: 0, geometryReads: 0, fitPasses: 0, resizeCallbacks: 0,
  resizeSends: 0, maxFitsPerFrame: 0, compositionStarts: 0, compositionEnds: 0,
  outputChunks: 0, physicalMoves: 0, deferredMoves: 0, blurEvents: 0,
};
let composing = false;
let framePending = false;
let currentSlot = 'column1';
let pendingSlot;
let retainedSelection;
const fitsByFrame = new Map();
let socket;
let lastSize = '';
const originalTerm = term;
function sendSize() {
  if (socket?.readyState !== WebSocket.OPEN || measurements.fitPasses === 0) return;
  const size = `${term.cols}:${term.rows}`;
  if (size === lastSize) return;
  lastSize = size;
  socket.send(JSON.stringify({ type: 'resize', source: 'canonical', cols: term.cols, rows: term.rows }));
  measurements.resizeSends++;
}
function scheduleFit() {
  if (framePending || (guarded && composing)) return;
  framePending = true;
  requestAnimationFrame(timestamp => {
    framePending = false;
    if (guarded && composing) return;
    measurements.fitPasses++;
    const count = (fitsByFrame.get(timestamp) || 0) + 1;
    fitsByFrame.set(timestamp, count);
    measurements.maxFitsPerFrame = Math.max(measurements.maxFitsPerFrame, count);
    const selection = guarded ? (term.getSelectionPosition() || retainedSelection?.position) : undefined;
    const selectedText = term.getSelection() || retainedSelection?.text;
    fit.fit();
    // This probe uses a short, single-line selection whose buffer coordinates
    // are unchanged by reflow. General wrapped selection anchoring needs more work.
    if (selection && selectedText) term.select(selection.start.x, selection.start.y, selectedText.length);
    sendSize();
  });
}
function place(slot) {
  currentSlot = slot;
  document.querySelector('#fill').style.visibility = slot === 'fill' ? 'visible' : 'hidden';
  const target = document.getElementById(slot);
  if (approach === 'stable') {
    measurements.geometryReads++;
    const rect = target.getBoundingClientRect();
    Object.assign(host.style, { left: `${rect.left + 2}px`, top: `${rect.top + 2}px`, width: `${rect.width - 4}px`, height: `${rect.height - 4}px` });
  } else {
    target.appendChild(host);
  }
}
place('column1');
term.open(host);
term.textarea.setAttribute('aria-label', 'Fake terminal input');
const textarea = term.textarea;
textarea.addEventListener('blur', () => { measurements.blurEvents++; });
textarea.addEventListener('compositionstart', () => { composing = true; measurements.compositionStarts++; });
textarea.addEventListener('compositionend', () => { composing = false; measurements.compositionEnds++; });
const observer = new ResizeObserver(() => { measurements.resizeCallbacks++; scheduleFit(); });
// Only this host is observed. Inactive slots and read-only placements never fit/send.
observer.observe(host);
const connection = await (await fetch('/connection')).json();
socket = new WebSocket(connection.url);
const originalSocket = socket;
term.onData(text => socket.send(JSON.stringify({ type: 'input', text })));
socket.onopen = () => { sendSize(); };
socket.onmessage = event => term.write(event.data, () => { measurements.outputChunks++; });
const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
const api = {
  get ready() { return measurements.outputChunks > 1 && !framePending; },
  prepare() {
    term.scrollToTop();
    term.select(0, 0, 'SCROLLBACK-SENTINEL'.length);
    term.focus();
  },
  beginComposition() {
    textarea.value = '';
    textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    textarea.value = '日本';
    textarea.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: '日本' }));
  },
  async move(slot) {
    measurements.moves++;
    if (guarded && composing && approach === 'reparent') {
      pendingSlot = slot; measurements.deferredMoves++;
    } else { place(slot); measurements.physicalMoves++; }
    // Reparenting blurs the textarea in Chromium; restore focus explicitly.
    term.focus();
    await frames();
    document.querySelector('#status').textContent = `${approach}: ${slot}, move ${measurements.moves}`;
  },
  async commit() {
    if (guarded) retainedSelection = { position: term.getSelectionPosition(), text: term.getSelection() };
    textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '日本' }));
    // Let xterm's composition finalizer consume the textarea before a blur/move.
    await new Promise(resolve => setTimeout(resolve, 0));
    const reparented = Boolean(pendingSlot);
    if (pendingSlot) { place(pendingSlot); pendingSlot = undefined; measurements.physicalMoves++; }
    term.focus();
    // A sees a resize while composing and defers its fit. B's move triggers
    // the observer after commit; scheduling here too would fit twice.
    if (!reparented) scheduleFit();
    await frames();
  },
  async resizeInactiveSlots() {
    for (const id of ['column1', 'column2', 'fill']) {
      if (id !== currentSlot) document.getElementById(id).style.width = '200px';
    }
    await frames();
  },
  snapshot() {
    const history = [];
    for (let i = 0; i < term.buffer.active.length; i++) history.push(term.buffer.active.getLine(i).translateToString(true));
    return {
      ...measurements, currentSlot, sameTerm: term === originalTerm,
      sameSocket: socket === originalSocket, sameTextarea: term.textarea === textarea,
      sameParent: host.parentNode === parent, selection: term.getSelection(),
      focused: document.activeElement === textarea, composing, preedit: textarea.value, history,
    };
  },
};
window.probe = api;
for (const button of document.querySelectorAll('button')) button.onclick = () => api.move(button.dataset.slot);
window.addEventListener('resize', () => { place(currentSlot); });
window.addEventListener('pagehide', () => { observer.disconnect(); socket.close(); term.dispose(); }, { once: true });
