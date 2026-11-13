import { Button, Dialog } from './ui';

export default function TerminalPasteDialog({ value, change, write, send }: {
  value: string; change: (value: string | null) => void; write: boolean; send: (text: string) => void;
}) {
  return <Dialog open onClose={() => change(null)} title="Paste terminal text" footer={<><Button onClick={() => change(null)}>Cancel</Button><Button disabled={!write || !value} onClick={() => { send(value); change(null); }}>Paste into terminal</Button></>}><p>Review before sending. Newlines can submit commands or answers.</p><textarea aria-label="Text to paste" value={value} onChange={event => change(event.target.value)} maxLength={60000} className="h-40 w-full bg-panel font-mono text-xs" /></Dialog>;
}
