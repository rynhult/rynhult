import { useCallback, useEffect, useRef, useState } from 'react';
import type { HostedTerminal } from './terminalHostLayer';
import { Button } from './ui';

export default function TerminalPhoneKeys({ resource, write, visible, paste }: {
  resource?: HostedTerminal; write: boolean; visible: boolean; paste: () => void;
}) {
  const [ctrl, setCtrl] = useState(false);
  const ctrlPending = useRef(false);
  const timeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const disarm = useCallback(() => {
    clearTimeout(timeout.current); timeout.current = undefined;
    ctrlPending.current = false; setCtrl(false);
  }, []);
  useEffect(() => { if (!visible || !write) disarm(); }, [visible, write, disarm]);
  useEffect(() => {
    const visibility = () => { if (document.hidden) disarm(); };
    window.addEventListener('blur', disarm);
    document.addEventListener('visibilitychange', visibility);
    resource?.host.addEventListener('focusout', disarm);
    return () => {
      window.removeEventListener('blur', disarm);
      document.removeEventListener('visibilitychange', visibility);
      resource?.host.removeEventListener('focusout', disarm);
      disarm();
    };
  }, [resource, disarm]);
  useEffect(() => {
    if (!resource?.terminal) return;
    resource.setInputTransform(data => {
      if (!ctrlPending.current || !/^[a-z]$/i.test(data) || resource.composing) return data;
      disarm();
      return String.fromCharCode(data.toUpperCase().charCodeAt(0) - 64);
    });
    return () => resource.setInputTransform(undefined);
  }, [resource, resource?.terminal, disarm]);
  return <><div role="toolbar" className="flex flex-wrap gap-2" aria-label="Terminal keys"><Button size="sm" disabled={!write} aria-pressed={ctrl} className={ctrl ? 'bg-accent text-accent-ink' : ''} onPointerDown={event => event.preventDefault()} onClick={() => { if (ctrlPending.current) { disarm(); return; }
    resource?.terminal?.focus(); ctrlPending.current = true; setCtrl(true);
    timeout.current = setTimeout(disarm, 10000); }}>Ctrl</Button>{[['⏎', '\r', 'Enter'], ['Esc', '\x1b', 'Esc'], ['^C', '\x03', '^C (interrupt)'], ['↑', '\x1b[A', 'Up'], ['↓', '\x1b[B', 'Down'], ['Tab', '\t', 'Tab']].map(([label, bytes, name]) => <Button key={name} size="sm" disabled={!write} aria-label={name} onPointerDown={event => event.preventDefault()} onClick={() => resource?.send(bytes)}>{label}</Button>)}</div>
    <details><summary className="cursor-pointer text-xs">More terminal keys</summary><div className="flex flex-wrap gap-2 py-2">{[['←', '\x1b[D'], ['→', '\x1b[C'], ['Page Up', '\x1b[5~'], ['Page Down', '\x1b[6~'], ['Ctrl U (clear line)', '\x15'], ['Ctrl D', '\x04']].map(([label, bytes]) => <Button key={label} size="sm" disabled={!write} onPointerDown={event => event.preventDefault()} onClick={() => resource?.send(bytes)}>{label}</Button>)}<Button size="sm" disabled={!write} onClick={paste}>Paste text…</Button></div></details></>;
}
