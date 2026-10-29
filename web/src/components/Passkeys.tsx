import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound } from 'lucide-react';
import { ApiError, api, post } from '../lib/api';
import { OFFLINE_TITLE, useOffline } from '../lib/queries';
import type { AuthState } from '../lib/types';
import { passkeyCeremony, passkeysAvailable } from '../lib/passkeys';
import { useConfirm } from './confirm';
import { Button, Card, CardHeader, Dialog, ErrorNote, Field, Input, LoadingRows, QueryState, Select } from './ui';

interface Key { id: number; name: string; rp_id: string; last_used_at: string | null }
interface Policy { owners: string; members: string }
interface State { passkeys: Key[]; recovery_code: boolean; policy: Policy; company_only?: boolean; enrollment_until?: string | null }

export function Passkeys({ owner }: { owner: boolean }) {
  const client = useQueryClient();
  const confirm = useConfirm();
  const offline = useOffline();
  const query = useQuery({ queryKey: ['passkeys'], queryFn: () => api<State>('/api/auth/passkeys') });
  const enrollmentUntil = query.data?.enrollment_until;
  const [enrollmentExpired, setEnrollmentExpired] = useState(false);
  useEffect(() => {
    const end = Date.parse(enrollmentUntil ?? '');
    const check = () => setEnrollmentExpired(!(end > Date.now()));
    check();
    const timer = Number.isFinite(end) && end > Date.now() ? setTimeout(check, end - Date.now()) : undefined;
    document.addEventListener('visibilitychange', check);
    window.addEventListener('focus', check);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', check); window.removeEventListener('focus', check); };
  }, [enrollmentUntil]);
  const enrollmentOpen = !!enrollmentUntil && !enrollmentExpired && Date.parse(enrollmentUntil) > Date.now();
  // Company sign-in is mentioned only where the sign-in page offers it (first-run review).
  const auth = useQuery({ queryKey: ['auth'], queryFn: () => api<AuthState>('/api/auth/state'), staleTime: 60000 });
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [deadline, setDeadline] = useState<number | null>(null);
  const [expired, setExpired] = useState(false);
  const verification = useRef<HTMLDivElement>(null);
  const verified = deadline !== null && Date.now() < deadline;
  const markVerified = () => { setDeadline(Date.now() + 300000); setExpired(false); };
  useEffect(() => {
    const check = () => { if (deadline !== null && Date.now() >= deadline) { setDeadline(null); setExpired(true); } };
    const timer = setTimeout(check, Math.max(0, (deadline ?? Date.now()) - Date.now()));
    document.addEventListener('visibilitychange', check);
    window.addEventListener('focus', check);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', check); window.removeEventListener('focus', check); };
  }, [deadline]);
  const verifyFirst = () => {
    if (verified) return false;
    verification.current?.scrollIntoView({ block: 'center' }); verification.current?.focus();
    return true;
  };
  const [recovery, setRecovery] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<Key | null>(null);
  const [draftName, setDraftName] = useState('');
  const blocked = busy || offline;
  const [policy, setPolicy] = useState<Policy | null>(null);
  const run = async (action: () => Promise<unknown>, needsVerification = false) => {
    if (needsVerification && verifyFirst()) return;
    if (blocked) return;
    setBusy(true); setError(null);
    try { await action(); await client.invalidateQueries({ queryKey: ['passkeys'] }); }
    catch (err) { if (err instanceof ApiError && err.status === 403 && /[Vv]erif/.test(err.message)) { setDeadline(null); setExpired(true); setEnrollmentExpired(true); } setError(err); }
    finally { setBusy(false); }
  };
  return <Card>
    <CardHeader title="Passkeys" icon={KeyRound} subtitle="Sign in with your fingerprint, face or a security key, instead of your password and authenticator code." />
    <div className="space-y-4 p-4">
      <p className="text-sm text-muted">A passkey works only at the address you use for Rynhult now; a different address needs its own. Add a second one (your phone or a security key) in case you lose this device, and save a recovery code somewhere offline.{auth.data?.sso ? ' People who sign in with your company account recover through company sign-in instead.' : ''}</p>
      {!passkeysAvailable() && <p className="text-sm text-muted">This browser needs a trusted HTTPS connection and passkey support.</p>}
      <QueryState query={query} loading={<LoadingRows rows={2} />}>
        {data => <>
          {data.passkeys.length === 0 && <p className="text-sm text-muted">No passkeys yet.</p>}
          <ul className="divide-y divide-line">
            {data.passkeys.map(key => <li key={key.id} className="flex flex-wrap items-center gap-2 py-2">
              <div className="min-w-0 flex-1"><p className="break-words text-sm">{key.name}</p><p className="break-all text-xs text-muted">{key.rp_id} · {key.last_used_at ? `Used ${new Date(key.last_used_at).toLocaleDateString()}` : 'Not used yet'}</p></div>
              <Button title={offline ? OFFLINE_TITLE : undefined} size="sm" disabled={blocked} aria-label={`Rename ${key.name}`} onClick={() => { if (verifyFirst()) return; setRenaming(key); setDraftName(key.name); }}>Rename</Button>
              <Button title={offline ? OFFLINE_TITLE : undefined} size="sm" variant="danger" disabled={blocked} aria-label={`Remove ${key.name}`} onClick={async () => {
                if (verifyFirst()) return;
                if (await confirm({ title: 'Remove passkey?', body: `You will no longer be able to sign in with ${key.name}. Verify your sign-in first.`, confirm: 'Remove passkey', danger: true }))
                  await run(() => post(`/api/auth/passkeys/${key.id}/remove`), true);
              }}>Remove</Button>
            </li>)}
          </ul>
          {data.company_only && <div className="space-y-2 border-t border-line pt-4" role="status">
            <h3 className="font-semibold">You sign in with your company account</h3>
            {enrollmentOpen
              ? <p className="text-sm text-muted">Your company sign-in proved multi-factor authentication, so you can add one passkey now without verifying again, until {new Date(enrollmentUntil ?? '').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.</p>
              : <p className="text-sm text-muted">To add a passkey, sign out and sign in again with your company account using multi-factor authentication, then add it here within five minutes. Your provider must confirm authentication within the last ten minutes. With a passkey, verify with it below to add more.</p>}
          </div>}
          <div ref={verification} tabIndex={-1} className="space-y-3 border-t border-line pt-4"><h3 className="font-semibold">1. Verify your sign-in</h3>
            <p className="text-sm text-muted">{data.company_only ? 'Verify with a passkey you already have before adding another, removing one or creating recovery material. Verification lasts five minutes.' : 'Verify again before adding or removing a passkey, creating recovery material, or changing policy. Verification lasts five minutes.'}</p>
            <Button title={offline ? OFFLINE_TITLE : undefined} disabled={blocked || !passkeysAvailable()} onClick={() => run(async () => { await passkeyCeremony('verify'); markVerified(); })}>Verify with a passkey</Button>
            {!data.company_only && <form className="grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); void run(async () => { await post('/api/auth/passkeys/reauth', { password, code }); setPassword(''); setCode(''); markVerified(); }); }}>
              <Field label="Verification password"><Input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></Field>
              <Field label="Verification authenticator code"><Input autoComplete="one-time-code" inputMode="numeric" maxLength={6} value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} /></Field>
              <Button title={offline ? OFFLINE_TITLE : undefined} type="submit" disabled={blocked || !password || code.length !== 6} className="sm:col-span-2">Verify with password and authenticator</Button>
            </form>}
            {verified && <p className="text-sm text-muted">Verified for five minutes.</p>}
            {expired && <p className="text-sm text-wait">Verification expired. Verify again to make changes.</p>}
          </div>
          <h3 className="font-semibold">2. Add a passkey</h3>
          {!verified && !enrollmentOpen && <p id="passkey-verify-first" className="text-sm text-muted">Verify first. Actions below take you to sign-in verification before making changes.</p>}
          <form aria-describedby={!verified && !enrollmentOpen ? 'passkey-verify-first' : undefined} className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); void run(async () => { await passkeyCeremony('register', name); setName(''); }, !enrollmentOpen); }}>
            <Field label="Passkey name"><Input maxLength={100} value={name} onChange={e => setName(e.target.value)} placeholder="Laptop or spare security key" /></Field>
            <Button variant="primary" title={offline ? OFFLINE_TITLE : undefined} type="submit" disabled={blocked || !name.trim() || !passkeysAvailable()}>Add passkey</Button>
          </form>
          {!data.company_only && <div aria-describedby={!verified ? 'passkey-verify-first' : undefined} className="space-y-2 border-t border-line pt-4"><h3 className="font-semibold">3. Save a recovery code</h3>
            <p className="text-sm text-muted">Recovery code: {data.recovery_code ? 'created' : 'not created'}. Your password plus this one-use code can recover lost passkeys, including when required.</p><p className="text-sm text-muted">Generating a code replaces the previous code. Store it offline.</p><p className="text-sm text-muted">Using recovery removes your passkeys and ends your sign-ins. Owner recovery allows password + authenticator fallback for owners. Members under Require must first ask an owner to allow fallback.</p>
            <Button title={offline ? OFFLINE_TITLE : undefined} disabled={blocked} onClick={async () => {
              if (verifyFirst()) return;
              if (await confirm({ title: 'Create recovery code?', body: 'This replaces your previous code. Store the new code offline; Rynhult shows it only once.', confirm: 'Create recovery code' }))
                await run(async () => setRecovery((await post<{ code: string }>('/api/auth/passkeys/recovery-code')).code), true);
            }}>Create recovery code</Button>
          </div>}
          {owner && <form className="grid gap-3 border-t border-line pt-4 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); void run(() => post('/api/auth/passkeys/policy', policy ?? data.policy), true); }}>
            <h3 className="font-semibold sm:col-span-2">Owner policy</h3><p className="text-sm text-muted sm:col-span-2">Require needs two passkeys per owner and one per member. Anyone with a Rynhult password also needs a recovery code. Password + authenticator sign-in remains available with Allow. Temporarily select Allow before inviting or resetting someone in a required role.</p>
            {(['owners', 'members'] as const).map(role => <Field key={role} label={`Passkeys for ${role}`}><Select value={(policy ?? data.policy)[role]} onChange={e => setPolicy({ ...(policy ?? data.policy), [role]: e.target.value })}><option value="allow">Allow</option><option value="require">Require</option></Select></Field>)}
            <Button variant="primary" title={offline ? OFFLINE_TITLE : undefined} type="submit" disabled={blocked} className="sm:col-span-2">Save passkey policy</Button>
          </form>}
        </>}
      </QueryState>
      <ErrorNote error={error} />
    </div>
    <Dialog open={!!renaming} onClose={() => setRenaming(null)} title="Rename passkey" dismissible={false}>
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); void run(async () => { await post(`/api/auth/passkeys/${renaming!.id}/name`, { name: draftName }); setRenaming(null); }, true); }}>
        <Field label="New passkey name"><Input value={draftName} maxLength={100} onChange={e => setDraftName(e.target.value)} autoFocus /></Field>
        <ErrorNote error={error} />
        <Button variant="primary" title={offline ? OFFLINE_TITLE : undefined} type="submit" disabled={blocked || !draftName.trim()}>Save name</Button>
      </form>
    </Dialog>
    <Dialog open={!!recovery} onClose={() => setRecovery(null)} title="Save your recovery code" dismissible={false}>
      <div className="space-y-3"><p className="text-sm text-muted">Store this offline. It is shown once and replaces any previous recovery code.</p><code className="block break-all select-all rounded border border-line p-3">{recovery}</code><Button onClick={() => setRecovery(null)}>I saved the code</Button></div>
    </Dialog>
  </Card>;
}
