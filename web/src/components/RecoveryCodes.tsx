import { Button } from './ui';

export function RecoveryCodes({ codes, onSaved }: { codes: string[]; onSaved: () => void }) {
  return <section className="space-y-3" aria-label="Recovery codes">
    <h2 className="font-semibold">Save your recovery codes</h2>
    <p>Store these offline with your password. Each code replaces your authenticator or passkey at sign-in once. These codes are shown only now; creating a new set invalidates this set.</p>
    <pre className="select-all overflow-auto rounded border border-line p-3">{codes.join('\n')}</pre>
    <Button onClick={onSaved}>I saved the codes</Button>
  </section>;
}
