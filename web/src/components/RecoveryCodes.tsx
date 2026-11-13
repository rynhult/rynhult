import { useState } from 'react';
import { Button, ErrorNote } from './ui';

export function RecoveryCodes({ codes, onSaved }: { codes: string[]; onSaved: () => void | Promise<void> }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  return <section className="space-y-3" aria-label="Recovery codes">
    <h2 className="font-semibold">Save your recovery codes</h2>
    <p>Store these offline with your password. Each code replaces your authenticator or passkey at sign-in once. These codes are shown only now; creating a new set invalidates this set.</p>
    <pre className="select-all overflow-auto rounded border border-line p-3">{codes.join('\n')}</pre>
    <div className="flex flex-wrap gap-2">
      <Button onClick={async () => { try { await navigator.clipboard.writeText(codes.join('\n')); } catch (err) { setError(err); } }}>Copy recovery codes</Button>
      <a className="inline-flex min-h-11 items-center underline" download="rynhult-recovery-codes.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(codes.join('\n'))}`}>Download recovery codes</a>
      <Button disabled={busy} onClick={async () => { setBusy(true); setError(null); try { await onSaved(); } catch (err) { setError(err); } finally { setBusy(false); } }}>{busy ? 'Saving…' : 'I saved the codes'}</Button>
    </div><ErrorNote error={error} />
  </section>;
}
