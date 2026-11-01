import { lazyImport } from '../lib/lazyImport';
type BindTextInput = typeof import('../lib/safariTerminalInput').bindSafariTextInput;
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import clsx from "clsx";
import { Keyboard, Lock, Maximize2, Minimize2, RotateCw, Unlock } from "lucide-react";
import { announce, Button, Dialog, Dot, Kbd } from "./ui";
import { api, ApiError } from "../lib/api";

import { useTerminalExpansion } from './terminalExpansion';
import type { ComponentProps } from 'react';

type Status = { kind: "connecting" } | { kind: "open"; write: boolean } | { kind: "closed"; reason: string };

/** Phone key bar (UX review PH-1): the keys that finish or stop an answer come first and always fit a 390px screen; the
    rest sit behind "More keys". [label, bytes, accessible name] */
const KEYS: [string, string, string][] = [
  ["⏎", "\r", "Enter"], ["Esc", "\x1b", "Esc"], ["^C", "\x03", "^C (interrupt)"], ["↑", "\x1b[A", "Up"], ["↓", "\x1b[B", "Down"], ["Tab", "\t", "Tab"],
];
const MORE_KEYS: [string, string, string][] = [
  ["←", "\x1b[D", "Left"], ["→", "\x1b[C", "Right"], ["PgUp", "\x1b[5~", "PgUp (Page up)"], ["PgDn", "\x1b[6~", "PgDn (Page down)"],
];
/** F6 leaves the terminal (Shift+F6 goes back): xterm keeps Tab for the CLI, so keyboard users need a key that it
    doesn't. F6 is the usual "next pane" key and CLIs rarely use it (UX review SE-9). */
export const LEAVE_KEY = "F6";
function leave(from: HTMLElement, back: boolean) {
  const focusable = [...document.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter((el) => !from.contains(el) && el.offsetParent !== null);
  const next = back
    ? focusable.filter((el) => el.compareDocumentPosition(from) & Node.DOCUMENT_POSITION_FOLLOWING).pop()
    : focusable.find((el) => el.compareDocumentPosition(from) & Node.DOCUMENT_POSITION_PRECEDING);
  (next ?? document.querySelector<HTMLElement>("#main"))?.focus();
}

const READER_PREFERENCE = "acc-terminal-screen-reader";
function readerPreference() {
  try { return localStorage.getItem(READER_PREFERENCE) !== "off"; } catch { return true; }
}

/** Read parsed cells, not PTY bytes: omit ANSI escapes and join soft-wrapped rows. */
function recentOutput(xterm: XTerm | null) {
  if (!xterm) return "";
  const buffer = xterm.buffer.active;
  let text = "";
  for (let i = 0; i < buffer.length; i++) {
    const line = buffer.getLine(i);
    if (!line) continue;
    text += (i && !line.isWrapped ? "\n" : "") + line.translateToString(!buffer.getLine(i + 1)?.isWrapped);
  }
  return text.trimEnd();
}

const keyClass = "min-h-11 min-w-11 shrink-0 rounded-md border border-line-strong bg-panel-2 px-2 text-[13px] text-fg";

/** The session's real terminal. `compact`: a tile in the grid (smaller text, no focus grab, no footer hint). */
// Polling parents pass primitives; their status refreshes must never render the
// imperative terminal subtree or disturb a browser's native composition editor.
export const Terminal = memo(function Terminal({ sessionId, live, compact, name, tone, stateLabel, onExpansionChange }: { sessionId: string; live: boolean; compact?: boolean; name?: string; tone?: ComponentProps<typeof Dot>['tone']; stateLabel?: string; onExpansionChange?: (expanded: boolean) => void }) {
  const expansion=useTerminalExpansion(sessionId,!!compact,!!name,onExpansionChange);
  const {expanded}=expansion;
  const expandedRef=useRef(expanded);
  expandedRef.current=expanded;
  const readerVisible=!compact||expanded;
  const longPress = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const touchOrigin = useRef({x:0,y:0});
  const selectionText = useRef<HTMLPreElement>(null);
  const outputText = useRef<HTMLTextAreaElement>(null);
  const selectFallback = useRef(false);
  const [selectionView, setSelectionView] = useState<string | null>(null);
  const [selection, setSelection] = useState<{text:string; x:number; y:number} | null>(null);
  const mac = /mac/i.test((navigator as Navigator & {userAgentData?: {platform?: string}}).userAgentData?.platform || navigator.platform);
  const selectionHint = mac ? 'Hold ⌥ Option while dragging to select text' : 'Hold Shift while dragging to select text';
  const copyShortcut = mac ? '⌘C' : 'Ctrl+Shift+C';
  const selectTitle = `${selectionHint}, or open a selectable text view. Copy with ${copyShortcut}.`;
  const copyTitle = `Copy selected text (${copyShortcut})`;
  const hintPanel = useRef<HTMLDivElement>(null);
  const hintShown = useRef(false);
  const dragOrigin = useRef<{x:number;y:number;id:number} | null>(null);
  const [showSelectionHint, setShowSelectionHint] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const socket = useRef<WebSocket | null>(null);
  const term = useRef<XTerm | null>(null);
  const [status, setStatus] = useState<Status>({ kind: "connecting" });
  const [screenReader, setScreenReader] = useState(readerPreference);
  const readerControl = useRef<HTMLInputElement>(null);
  const [output, setOutput] = useState<string | null>(null);
  const [outputInfo, setOutputInfo] = useState("");
  const [outputBusy, setOutputBusy] = useState(false);
  const outputRequest = useRef(0);
  const [copyError, setCopyError] = useState("");
  const [ctrl, setCtrl] = useState(false);
  const [moreKeys, setMoreKeys] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [take, setTake] = useState(false);
  const touch = typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches;

  useEffect(() => {
    const mount = (bindSafariTextInput?: BindTextInput) => {
      if (!host.current) return;
      const xterm = new XTerm({
        fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace',
        fontSize: touch || compact ? 12 : 13,
        lineHeight: 1.15,
        cursorBlink: true,
        scrollback: 10000,
        allowProposedApi: true,
        macOptionClickForcesSelection: true,
        screenReaderMode: (!compact || expandedRef.current) && readerPreference(),
        theme: { background: "#0b0d10", foreground: "#e6e8eb", cursor: "#7aa2ff", selectionBackground: "#2b3a5a" },
      });
      const fit = new FitAddon();
      xterm.loadAddon(fit);
      xterm.loadAddon(new WebLinksAddon((_, uri) => window.open(uri, "_blank", "noopener")));
      xterm.open(host.current);
      for (const [name, value] of Object.entries({ autocomplete: 'off', autocorrect: 'off', spellcheck: 'false',
        autocapitalize: 'off', writingsuggestions: 'false' })) xterm.textarea?.setAttribute(name, value);
      const disposeTextInput = bindSafariTextInput?.(xterm, host.current);
      if (!compact || expandedRef.current) xterm.textarea?.setAttribute("aria-describedby", `${expandedRef.current ? "expanded-leave" : "leave"}-${sessionId}`);
      // Screen readers read the rows on demand; xterm's own assertive region would read the agent's stream aloud as it
      // arrives. State changes are announced once through Rynhult's Announcer instead.
      host.current.querySelector(".live-region")?.setAttribute("aria-live", "off");
      fit.fit();
      term.current = xterm;
      const selected = xterm.onSelectionChange(() => {
        const text = xterm.getSelection().split(/\r?\n/).map(line => line.trimEnd()).join('\n');
        const bounds = host.current?.getBoundingClientRect();
        const end = xterm.getSelectionPosition()?.end;
        setSelection(text && bounds ? {text, x:bounds.left + (end?.x ?? 0) * bounds.width / xterm.cols,
          y:bounds.top + Math.max(0, Math.min(xterm.rows - 1, (end?.y ?? 0) - xterm.buffer.active.viewportY)) * bounds.height / xterm.rows} : null);
      });

      const proto = location.protocol === "https:" ? "wss" : "ws";
      const params = new URLSearchParams({ mode: "write", rows: String(xterm.rows), cols: String(xterm.cols), ...(take ? { take: "1" } : {}) });
      const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${sessionId}/tty?${params}`);
      ws.binaryType = "arraybuffer";
      socket.current = ws;
      setStatus({ kind: "connecting" });

      const send = (data: string) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ t: "i", d: data }));
      let sentSize = '';
      const resize = () => {
        const dims = fit.proposeDimensions();
        if (!dims || !Number.isFinite(dims.rows) || !Number.isFinite(dims.cols)) return;
        if (dims.rows !== xterm.rows || dims.cols !== xterm.cols) fit.fit();
        const size = `${xterm.rows}:${xterm.cols}`;
        // ResizeObserver may notify after surrounding status/layout updates even
        // when character dimensions did not change. Keep the PTY/provider out
        // of surrounding status updates that do not resize the actual editor.
        if (ws.readyState === WebSocket.OPEN && size !== sentSize) {
          ws.send(JSON.stringify({ t: "r", rows: xterm.rows, cols: xterm.cols }));
          sentSize = size;
        }
      };
      ws.onopen = resize;
      ws.onmessage = (e) => {
        if (typeof e.data === "string") {
          const message = JSON.parse(e.data);
          if (message.t === "mode") setStatus({ kind: "open", write: message.write });
          if (message.t === "closed") setStatus({ kind: "closed", reason: message.reason });
          return;
        }
        xterm.write(new Uint8Array(e.data));
      };
      ws.onclose = (e) => {
        const reasons: Record<number, string> = {
          4001: "Another window took control of this terminal.",
          4401: "Your login expired.",
          4403: "Terminal access denied.",
          4404: "This session has no terminal right now.",
        };
        setStatus((s) => (s.kind === "closed" ? s : { kind: "closed", reason: reasons[e.code] ?? "Disconnected." }));
      };
      const input = xterm.onData((data) => {
        if (ctrlRef.current && data.length === 1) {
          const code = data.toUpperCase().charCodeAt(0);
          if (code >= 64 && code < 96) data = String.fromCharCode(code - 64);
          setCtrl(false);
        }
        send(data);
      });
      const observer = new ResizeObserver(() => resize());
      observer.observe(host.current);
      // A late lazy mount must not take focus back from a tab or another control.
      if (!compact && !touch && document.activeElement === document.body) xterm.focus();
      return () => {
        outputRequest.current++;
        observer.disconnect();
        input.dispose();
        selected.dispose();
        disposeTextInput?.();
        ws.close();
        xterm.dispose();
      };
    };
    let cancelled = false;
    let dispose: (() => void) | undefined;
    if (/^((?!chrome|chromium|android).)*safari/i.test(navigator.userAgent)) {
      // Install native text ownership before creating/focusing the editor or
      // opening its socket. A slow chunk load cannot expose an unpatched editor.
      void lazyImport(() => import('../lib/safariTerminalInput')).then(module => {
        if (!cancelled) dispose = mount(module.bindSafariTextInput);
      }).catch(() => {
        if (!cancelled) setStatus({ kind: 'closed', reason: 'Terminal input could not be loaded. Reload to try again.' });
      });
    } else dispose = mount();
    return () => { cancelled = true; dispose?.(); };
  }, [sessionId, generation, take, touch, compact]);

  useEffect(() => {
    if (!term.current) return;
    term.current.options.screenReaderMode = readerVisible && screenReader;
    if (readerVisible) term.current.textarea?.setAttribute('aria-describedby', `${expanded?'expanded-leave':'leave'}-${sessionId}`);
    else term.current.textarea?.removeAttribute('aria-describedby');
    host.current?.querySelector(".live-region")?.setAttribute("aria-live", "off");
  }, [screenReader, readerVisible, expanded, generation, sessionId, take, touch]);

  useEffect(() => {
    if (selectionView === null) return;
    const changed = () => {
      const native = window.getSelection();
      if (!native?.rangeCount || !selectionText.current?.contains(native.anchorNode) || !selectionText.current.contains(native.focusNode)) {
        setSelection(null);
        return;
      }
      const text = native.toString().split(/\r?\n/).map(line => line.trimEnd()).join('\n');
      const box = native.getRangeAt(0).getBoundingClientRect();
      setSelection(text ? {text, x:box.right, y:box.bottom} : null);
    };
    document.addEventListener('selectionchange', changed);
    return () => document.removeEventListener('selectionchange', changed);
  }, [selectionView]);
  useLayoutEffect(() => {
    if (output !== null && selectFallback.current && outputText.current) {
      selectFallback.current = false;
      outputText.current.focus();
      outputText.current.select();
    }
  }, [output]);
  const dismissSelectionHint = useCallback(() => {
    if (hintPanel.current?.contains(document.activeElement)) term.current?.focus();
    setShowSelectionHint(false);
  }, []);
  useEffect(() => {
    if (!showSelectionHint) return;
    const timer = window.setTimeout(dismissSelectionHint, 10000);
    return () => window.clearTimeout(timer);
  }, [showSelectionHint, dismissSelectionHint]);
  const beginSelectionDrag = (event: React.PointerEvent) => {
    dragOrigin.current = null;
    const mode = term.current?.modes.mouseTrackingMode;
    if (hintShown.current || event.pointerType !== 'mouse' || event.button !== 0 || !mode || mode === 'none'
        || event.altKey || event.shiftKey || event.ctrlKey || event.metaKey) return;
    dragOrigin.current = {x:event.clientX,y:event.clientY,id:event.pointerId};
  };
  const trackSelectionDrag = (event: React.PointerEvent) => {
    const origin = dragOrigin.current;
    if (!origin || origin.id !== event.pointerId) return;
    if (!(event.buttons & 1) || event.altKey || event.shiftKey || event.ctrlKey || event.metaKey) {
      dragOrigin.current = null;
      return;
    }
    if (Math.hypot(event.clientX-origin.x,event.clientY-origin.y) < 6) return;
    dragOrigin.current = null;
    if (hintShown.current || !term.current || term.current.modes.mouseTrackingMode === 'none') return;
    hintShown.current = true;
    setShowSelectionHint(true);
    announce(selectionHint);
  };
  const cancelLongPress = () => clearTimeout(longPress.current);
  useEffect(() => () => clearTimeout(longPress.current), []);
  const startLongPress = (event: React.PointerEvent<HTMLPreElement>) => {
    if (event.pointerType !== 'touch') return;
    cancelLongPress();
    const {clientX:x,clientY:y} = event;
    touchOrigin.current = {x,y};
    longPress.current = setTimeout(() => {
      const native = window.getSelection();
      // Preserve the platform's selection/handles when it already handled the gesture.
      if (!native || !native.isCollapsed) return;
      const doc = document as Document & {
        caretPositionFromPoint?: (x:number,y:number) => {offsetNode:Node;offset:number} | null;
        caretRangeFromPoint?: (x:number,y:number) => Range | null;
      };
      const caret = doc.caretPositionFromPoint?.(x,y);
      const range = doc.caretRangeFromPoint?.(x,y);
      const node = caret?.offsetNode ?? range?.startContainer;
      const offset = caret?.offset ?? range?.startOffset ?? 0;
      if (!node || node.nodeType !== Node.TEXT_NODE || !selectionText.current?.contains(node)) return;
      const text = node.textContent ?? '';
      let start = Math.min(offset,Math.max(0,text.length-1)), end = start;
      const word = (char:string) => /[\p{L}\p{N}_]/u.test(char);
      while(start>0 && word(text[start-1])) start--;
      while(end<text.length && word(text[end])) end++;
      if(end===start) end=Math.min(text.length,start+1);
      const selected = document.createRange();
      selected.setStart(node,start);selected.setEnd(node,end);
      native.removeAllRanges();native.addRange(selected);
    },500);
  };
  const toggleSelection = () => {
    setShowSelectionHint(false);
    setSelection(null);
    term.current?.clearSelection();
    window.getSelection()?.removeAllRanges();
    setSelectionView(current => current === null ? recentOutput(term.current) : null);
  };
  const copySelection = async () => {
    if (!selection) return;
    const text = selection.text;
    try { await navigator.clipboard.writeText(text); announce('Terminal selection copied.'); }
    catch {
      outputRequest.current++;
      setOutputBusy(false);
      setCopyError('');
      selectFallback.current = true;
      setOutputInfo('Clipboard access was denied. Your selected text is selected below; use your browser’s Copy command.');
      setOutput(text);
    }
  };

  const toggleReader = (enabled: boolean) => {
    setScreenReader(enabled);
    try { localStorage.setItem(READER_PREFERENCE, enabled ? "on" : "off"); }
    catch { announce("Screen reader preference could not be saved in this browser."); }
  };
  const copyOutput = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyError("");
      announce("Recent terminal output copied.");
      return true;
    } catch {
      setCopyError("Copy failed. Open as text to select and copy the output.");
      announce("Copy failed. Open as text to select and copy the output.");
      return false;
    }
  };
  const readOutput = async () => {
    try {
      const result = await api<{ text: string; truncated: boolean }>(`/api/sessions/${sessionId}/terminal-output`, { cache: "no-store" });
      return { text: result.text, limited: false, note: result.truncated ? "Showing the last 200,000 characters of the captured scrollback." : "Snapshot of the terminal’s retained scrollback and current screen." };
    } catch (error) {
      const why = error instanceof ApiError && error.status === 429 ? "Too many snapshots in the last minute" : "Scrollback unavailable";
      return { text: recentOutput(term.current), limited: true, note: `${why}. Showing only output held in this browser; older lines may be missing.` };
    }
  };
  const refreshOutput = async () => {
    const request = ++outputRequest.current;
    setOutputBusy(true);
    setCopyError("");
    const result = await readOutput();
    if (request !== outputRequest.current) return;
    setOutput(result.text);
    setOutputInfo(result.note);
    setOutputBusy(false);
    announce("Terminal text snapshot refreshed. " + result.note);
  };
  const openOutput = () => {
    setOutput(recentOutput(term.current));
    setOutputInfo("Loading scrollback…");
    void refreshOutput();
  };
  const closeOutput = () => {
    outputRequest.current++;
    setOutputBusy(false);
    setOutput(null);
  };
  const copyRecentOutput = async () => {
    const result = await readOutput();
    const copied = await copyOutput(result.text);
    if (copied && result.limited) {
      setCopyError(result.note);
      announce(result.note);
    }
  };

  const announced = useRef("");
  useEffect(() => {
    if (compact) return;
    const text = status.kind === "open" ? (status.write ? "" : "Terminal is read-only: another window has control.")
      : status.kind === "closed" ? `Terminal closed. ${status.reason}` : "";
    if (text && text !== announced.current) announce(text);
    announced.current = text;
  }, [status, compact]);

  const ctrlRef = useRef(ctrl);
  ctrlRef.current = ctrl;
  // The key bar sends without focusing the terminal: focusing its text input opens the phone's keyboard, which a tap
  // on ↑ or Esc never should. Its buttons also keep focus where it was (an open keyboard stays open).
  const sendKey = (data: string) => {
    socket.current?.readyState === WebSocket.OPEN && socket.current.send(JSON.stringify({ t: "i", d: data }));
  };
  const keepFocus = { onPointerDown: (e: React.PointerEvent) => e.preventDefault(), onMouseDown: (e: React.MouseEvent) => e.preventDefault() };
  const reconnect = (takeControl: boolean) => {
    setTake(takeControl);
    setGeneration((g) => g + 1);
  };

  return (
    <div className="h-full min-h-0">
    <div ref={expansion.root} role={expanded?'dialog':undefined} aria-modal={expanded?true:undefined} aria-label={expanded?'Expanded terminal':undefined}
      style={expanded?{height:expansion.viewport.height,top:expansion.viewport.top}:undefined}
      className={clsx("flex h-full min-h-0 flex-col overflow-hidden border border-line bg-[#0b0d10]",expanded?"terminal-window-expanded":"relative rounded-lg")}
      onKeyDownCapture={event=>{
        const nestedDialog = (event.target as HTMLElement).closest('[role=dialog]');
        if ((!nestedDialog || nestedDialog === event.currentTarget) && selection && event.key.toLowerCase() === 'c' && !event.altKey && !event.nativeEvent.isComposing && (event.metaKey || (event.ctrlKey && event.shiftKey))) {
          event.preventDefault();event.stopPropagation();void copySelection();return;
        }
        if(!expanded||event.key!=='Tab'||(event.target as HTMLElement).closest('.xterm')||(event.target as HTMLElement).closest('[role=dialog]')!==event.currentTarget)return;
        const controls=[...event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), input, textarea, a[href], [tabindex="0"]')].filter(el=>el.offsetParent!==null);
        const next=event.shiftKey?controls.at(-1):controls[0];
        if((event.shiftKey&&document.activeElement===controls[0])||(!event.shiftKey&&document.activeElement===controls.at(-1))){event.preventDefault();next?.focus({preventScroll:true});}
      }}>
      {showSelectionHint&&<div ref={hintPanel} role="group" aria-label="Terminal selection hint" className="absolute left-2 right-2 top-2 z-10 flex flex-wrap items-center gap-2 rounded-md border border-line bg-panel px-3 py-2 text-xs text-fg shadow-pop">
        <span>{selectionHint}</span>
        <Button size="sm" title={selectTitle} onClick={()=>{toggleSelection();requestAnimationFrame(()=>selectionText.current?.focus({preventScroll:true}));}}>Select text</Button>
        <Button size="sm" aria-label="Dismiss selection hint" onClick={dismissSelectionHint}>Dismiss</Button>
      </div>}
      {expanded&&<div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-panel px-3 py-1.5 text-xs"
        onKeyDown={event=>{if(event.key.toLowerCase()==='c'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.nativeEvent.isComposing){event.preventDefault();event.stopPropagation();expansion.change(false);}}}>
        <span className="flex min-w-0 flex-1 items-center gap-2 max-sm:basis-full">
          <Dot tone={tone}/><span className="sr-only">{stateLabel}</span><span className="min-w-0 flex-1 truncate font-medium" title={name}>{name}</span>
        </span>
        <span id={`expanded-leave-${sessionId}`} className="sr-only sm:not-sr-only sm:text-faint"><Kbd>F6</Kbd> focuses this bar</span>
        <Button size="sm" title={selectTitle} aria-pressed={selectionView!==null} onClick={toggleSelection}>Select text</Button>
        {selection&&<Button size="sm" title={copyTitle} onClick={()=>void copySelection()}>Copy selected text</Button>}
        <Button data-collapse-terminal size="sm" aria-label="Collapse terminal" aria-pressed={true} aria-keyshortcuts="C" onClick={()=>expansion.change(false)}><Minimize2 size={13} aria-hidden="true"/>Collapse</Button><Kbd>C</Kbd>
        {document.fullscreenEnabled&&<Button size="sm" onClick={()=>void expansion.fullscreen()}>Full screen</Button>}
        {expansion.fullscreenError&&<span role="alert" className="w-full text-wait">{expansion.fullscreenError}</span>}
      </div>}
      {!compact && <a data-skip-terminal href={`#output-${sessionId}`} onClick={(e) => { e.preventDefault(); readerControl.current?.focus(); }}
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded-md focus:bg-panel focus:p-2 focus:text-accent focus:inline-flex focus:min-h-11 focus:items-center">Skip terminal</a>}
      <div className={clsx("flex flex-wrap items-center justify-between gap-2 border-b border-line bg-panel px-3 py-1.5 text-xs",expanded&&status.kind==='open'&&status.write&&"hidden")}>
        <span className="flex items-center gap-1.5 text-muted">
          {status.kind === "open" && (status.write
            ? !compact && <><Unlock size={13} className="text-ok" /> Read-write</>  /* a Grid tile stays quiet when all is well (GR-2) */
            : <><Lock size={13} className="text-wait" /> Read-only · another window has control</>)}
          {status.kind === "connecting" && "Connecting…"}
          {status.kind === "closed" && <span className="text-wait">{status.reason}</span>}
        </span>
        <span className="flex items-center gap-1">
          {!expanded&&(compact||!touch)&&<Button size="sm" title={selectTitle} aria-pressed={selectionView!==null} onClick={toggleSelection}>Select text</Button>}
          {name&&!expanded&&(compact||!touch)&&<Button data-expand-terminal size="sm" aria-label="Expand terminal" aria-pressed={false} onClick={()=>expansion.change(true)}><Maximize2 size={13} aria-hidden="true"/>Expand</Button>}
          {status.kind === "open" && !status.write && <Button size="sm" variant="secondary" onClick={() => reconnect(true)}>Take control</Button>}
          {status.kind === "closed" && live && (
            <Button size="sm" variant="secondary" onClick={() => reconnect(false)}><RotateCw size={13} /> Reconnect</Button>
          )}
          {!compact && <span className="inline-flex items-center gap-1 text-faint" id={`leave-${sessionId}`}><Kbd>{LEAVE_KEY}</Kbd> leaves the terminal</span>}
          {!compact && <span className="hidden text-faint lg:inline">· Closing this view never stops the session.</span>}
        </span>
      </div>
      <div className="relative min-h-0 flex-1">
      <div ref={host} onPointerDownCapture={beginSelectionDrag} onPointerMoveCapture={trackSelectionDrag}
        onPointerUpCapture={()=>{dragOrigin.current=null;}} onPointerCancelCapture={()=>{dragOrigin.current=null;}} className="h-full" style={selectionView!==null?{visibility:'hidden'}:undefined} aria-hidden={selectionView!==null?true:undefined} inert={selectionView!==null?true:undefined} onClick={() => { if(!term.current?.hasSelection()) term.current?.focus(); }}
        onKeyDownCapture={(event) => {
          // Capture before xterm: its textarea handler doesn't receive keys from accessibility rows.
          if (event.key !== LEAVE_KEY || event.ctrlKey || event.altKey || event.metaKey) return;
          event.preventDefault();
          event.stopPropagation();
          if(expanded)expansion.focusBar();
          else if(event.shiftKey&&!compact)expansion.root.current?.querySelector<HTMLAnchorElement>('[data-skip-terminal]')?.focus({preventScroll:true});
          else leave(event.currentTarget, event.shiftKey);
        }} />
      {selectionView!==null&&<pre ref={selectionText} role="region" aria-label="Selectable terminal text" tabIndex={0}
        onKeyDown={event=>{if(event.key===LEAVE_KEY&&!event.ctrlKey&&!event.altKey&&!event.metaKey){event.preventDefault();event.stopPropagation();if(expanded)expansion.focusBar();else leave(event.currentTarget,event.shiftKey);}}}
        className="absolute inset-0 m-0 overflow-auto whitespace-pre-wrap break-words p-2 font-mono text-xs text-[#e6e8eb] select-text"
        onPointerDown={startLongPress} onPointerUp={cancelLongPress} onPointerCancel={cancelLongPress}
        onPointerMove={event=>{if(Math.hypot(event.clientX-touchOrigin.current.x,event.clientY-touchOrigin.current.y)>10)cancelLongPress();}}
        style={{userSelect:'text',WebkitUserSelect:'text',touchAction:'auto'}}>{selectionView}</pre>}
      </div>
      {selection&&<Button size="sm" title={copyTitle} aria-label="Copy selection" onMouseDown={event=>event.preventDefault()} onClick={()=>void copySelection()}
        className="fixed z-10 min-h-11 shadow-pop"
        style={{left:Math.max(8,Math.min(selection.x,window.innerWidth-90)),top:Math.max(8,Math.min(selection.y,window.innerHeight-52))}}>Copy</Button>}
      {readerVisible && <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-line bg-panel px-3 py-1.5 text-xs">
        <label className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center gap-2 text-muted pointer-coarse:min-h-11">
          <input ref={readerControl} id={`output-${sessionId}`} type="checkbox" checked={screenReader}
            onChange={(e) => toggleReader(e.target.checked)} />Screen reader output
        </label>
        <Button size="sm" variant="secondary" onClick={openOutput}>Open as text</Button>
        <Button size="sm" variant="ghost" onClick={copyRecentOutput}>Copy recent output</Button>
        {touch&&!compact&&!expanded&&<>
          <Button size="sm" title={selectTitle} aria-pressed={selectionView!==null} onClick={toggleSelection}>Select text</Button>
          {name&&<Button data-expand-terminal size="sm" aria-label="Expand terminal" aria-pressed={false} onClick={()=>expansion.change(true)}><Maximize2 size={13} aria-hidden="true"/>Expand</Button>}
        </>}
        {copyError && <p className="w-full text-wait">{copyError}</p>}
      </div>}
      <Dialog open={output !== null} onClose={closeOutput} title="Recent terminal output" wide
        description="Recent output as selectable text. Refresh to include new output."
        footer={<>
          <Button variant="secondary" disabled={outputBusy} onClick={refreshOutput}>Refresh output</Button>
          <Button onClick={() => copyOutput(output ?? "")}>Copy recent output</Button>
        </>}>
        <p className="mb-3 text-xs text-muted">{outputBusy ? "Loading scrollback…" : outputInfo}</p>
        <label className="mb-2 block text-xs text-muted" htmlFor={`text-output-${sessionId}`}>Recent terminal output</label>
        <textarea ref={outputText} id={`text-output-${sessionId}`} readOnly value={output ?? ""} rows={14} spellCheck={false}
          className="w-full resize-y rounded-md border border-line-input bg-panel-2 p-3 font-mono text-xs text-fg" />
        {output === "" && !outputBusy && <p className="mt-2 text-xs text-muted">No terminal output received yet.</p>}
        {copyError && <p className="mt-2 text-xs text-wait">{copyError}</p>}
      </Dialog>
      {touch && (
        <div role="toolbar" aria-label="Terminal keys" className="flex flex-wrap gap-1.5 border-t border-line bg-panel px-2 py-1.5">
          {KEYS.map(([label, data, name]) => (
            <button key={name} type="button" aria-label={name === label ? undefined : name} onClick={() => sendKey(data)} {...keepFocus}
              className={clsx(keyClass, label === "⏎" && "border-accent/60 font-semibold")}>{label}</button>
          ))}
          <button type="button" aria-expanded={moreKeys} onClick={() => setMoreKeys(!moreKeys)} {...keepFocus} className={clsx(keyClass, "px-2.5 text-muted")}>More keys</button>
          {moreKeys && <>
            {/* Ctrl modifies the next key typed on the keyboard, so it alone opens it */}
            <button type="button" aria-pressed={ctrl} onClick={() => { setCtrl(!ctrl); term.current?.focus(); }}
              className={clsx(keyClass, ctrl && "border-accent bg-accent text-accent-ink")}>Ctrl</button>
            {MORE_KEYS.map(([label, data, name]) => (
              <button key={name} type="button" aria-label={name} onClick={() => sendKey(data)} {...keepFocus} className={keyClass}>{label}</button>
            ))}
            <button type="button" {...keepFocus} onClick={async () => { try { sendKey(await navigator.clipboard.readText()); } catch { const t = prompt("Paste"); if (t) sendKey(t); } }}
              className={clsx(keyClass, "inline-flex items-center gap-1")}><Keyboard size={13} aria-hidden="true" /> Paste</button>
          </>}
        </div>
      )}
    </div>
    </div>
  );
});
