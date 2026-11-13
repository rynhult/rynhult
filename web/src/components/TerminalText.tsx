import { Button, Dialog } from './ui';

export function TerminalText({ text, message, close, copy, refresh }: {
  text: string; message: string; close: () => void; copy: () => void; refresh: () => void;
}) {
  return <Dialog open onClose={close} title="Terminal output" footer={<>
    <Button onClick={refresh}>Refresh output</Button><Button onClick={copy}>Copy output</Button>
  </>}><p className="text-xs text-muted">Snapshot of this run's buffered output. Older lines may be missing.</p>
    {message && <p role="status">{message}</p>}
    <textarea readOnly aria-label="Terminal output" value={text} className="h-96 w-full bg-panel font-mono text-xs" />
  </Dialog>;
}
