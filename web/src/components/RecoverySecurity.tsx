import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { post } from '../lib/api';
import { passkeyCeremony, passkeysAvailable } from '../lib/passkeys';
import { RecoveryCodes } from './RecoveryCodes';
import { SettingsCard as Card, SettingsCardHeader as CardHeader } from './shell/SettingsSurfaces';
import { Button, ErrorNote, Field, Input } from './ui';

export function RecoverySecurity() {
  const client = useQueryClient();
  const [recovering, setRecovering] = useState(false);
  const [recoveryReady, setRecoveryReady] = useState(false);
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const generate = async (passkey: boolean) => {
    setBusy(true); setError(null);
    try {
      if (passkey) await passkeyCeremony('verify');
      else await post(recovering ? '/api/auth/passkeys/recovery-reauth' : '/api/auth/passkeys/reauth', { password, code });
      await client.invalidateQueries({ queryKey: ['passkeys'] });
      setPassword(''); setCode('');
      if (recovering && !passkey) { setRecoveryReady(true); return; }
      setCodes((await post<{ codes: string[] }>('/api/auth/passkeys/recovery-codes')).codes);
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  return <Card><CardHeader title="Sign-in recovery codes" subtitle="Keep access if you lose your authenticator or passkey." />
    <div className="space-y-3 p-4">
      <p>Verify again to create ten single-use codes. Creating a new set invalidates your previous set. Store these offline with your password.</p>
      <label className="flex gap-2"><input type="checkbox" checked={recovering} onChange={e => { setRecovering(e.target.checked); setRecoveryReady(false); setCode(''); }} />I signed in with a recovery code and lost my authenticator</label>
      {recovering && <p>Enter a different unused recovery code and your password. This opens a five-minute window for one action. Add a passkey first. Regenerating recovery codes instead ends the window; you would need another unused code to verify again. Other sensitive changes still require normal verification.</p>}
      {!recoveryReady && <form className="space-y-3" onSubmit={e => { e.preventDefault(); void generate(false); }}>
        <Field label="Recovery verification password"><Input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></Field>
        <Field label={recovering ? "Different unused recovery code" : "Recovery verification authenticator code"}><Input autoComplete="one-time-code" value={code} maxLength={recovering ? 100 : 6} onChange={e => setCode(e.target.value)} /></Field>
        <Button className="h-auto min-h-8 max-w-full whitespace-normal" type="submit" disabled={busy || !password || (recovering ? !code.trim() : code.length !== 6)}>{recovering ? 'Verify to recover sign-in' : 'Verify and create new recovery codes'}</Button>
      </form>}
      {recoveryReady && <div className="space-y-3">
        <h3 className="font-semibold">Add a passkey</h3>
        <p>Add a passkey before regenerating codes. Either action ends this recovery window. After adding a passkey, verify with it to create new recovery codes.</p>
        <Field label="Recovery passkey name"><Input value={name} maxLength={100} onChange={e => setName(e.target.value)} /></Field>
        <Button variant="primary" disabled={busy || !name.trim() || !passkeysAvailable()} onClick={async () => {
          setBusy(true); setError(null);
          try { await passkeyCeremony('register', name); setRecoveryReady(false); setName(''); await client.invalidateQueries({ queryKey: ['passkeys'] }); }
          catch (err) { setError(err); } finally { setBusy(false); }
        }}>Add a passkey</Button>
        <Button disabled={busy} onClick={async () => {
          setBusy(true); setError(null);
          try { setCodes((await post<{ codes: string[] }>('/api/auth/passkeys/recovery-codes')).codes); setRecoveryReady(false); await client.invalidateQueries({ queryKey: ['passkeys'] }); }
          catch (err) { setError(err); } finally { setBusy(false); }
        }}>Regenerate codes and end recovery window</Button>
      </div>}
      <Button className="h-auto min-h-8 max-w-full whitespace-normal" disabled={busy || !passkeysAvailable()} onClick={() => generate(true)}>Verify with passkey and create new recovery codes</Button>
      <ErrorNote error={error} />
      {codes && <RecoveryCodes codes={codes} onSaved={() => setCodes(null)} />}
    </div>
  </Card>;
}
