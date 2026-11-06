import { useState } from 'react';
import { Button, Dialog, Field, Select } from './ui';

/** Explicit human handover. The confirmation never clears or replays remote input. */
export function TerminalTakeDialog({ open, onClose, onTake }: { open: boolean; onClose: () => void; onTake: (reason: string) => void }) {
  const [reason, setReason] = useState('');
  return <Dialog open={open} onClose={onClose} title="Take terminal control" footer={<><Button onClick={onClose}>Cancel</Button><Button disabled={!reason} onClick={() => { onTake(reason); onClose(); }}>Take control</Button></>}>
    <p>The current controller becomes read-only. Partial input may remain in the terminal; inspect it before typing. Taking control does not clear, copy or replay that input. Unacknowledged bytes and active composition must finish first.</p>
    <Field label="Reason"><Select value={reason} onChange={event => setReason(event.target.value)}><option value="">Choose a reason</option><option value="manual_handover">Manual handover</option><option value="controller_unavailable">Controller unavailable</option><option value="team_intervention">Team intervention</option></Select></Field>
  </Dialog>;
}
