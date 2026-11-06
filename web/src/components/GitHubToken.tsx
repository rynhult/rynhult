import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { SecurityVerification } from './SecurityVerification';
import { Button, ErrorNote, Field, Input } from './ui';
import { Surface, SurfaceHeader } from './shell/PagePrimitives';

type Status = { set: boolean; suffix: string | null; login?: string; scopes?: string };
export function GitHubToken() {
  const state = useQuery({queryKey: ['github-token'], queryFn: () => api<Status>('/api/providers/github-token')});
  const [token, setToken] = useState(''), [action, setAction] = useState<'save' | 'remove' | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  const [identity, setIdentity] = useState<Status | null>(null);
  const change = async () => {
    const removing = action === 'remove';
    setAction(null); setBusy(true); setError(null);
    const value = token; setToken('');
    try {
      const result = await api<Status>('/api/providers/github-token', {method: removing ? 'DELETE' : 'PUT',
        ...(removing ? {} : {json: {token: value}})});
      setIdentity(result); await state.refetch();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Surface>
    <SurfaceHeader title="GitHub token" subtitle="Dashboard credential for repository views and creating repositories on GitHub. Stored on the main Rynhult, separately from agent sign-ins." />
    <div className="space-y-3 p-4">
      <p className="text-sm">{state.data ? (state.data.set ? `Set${state.data.suffix ? `, ends …${state.data.suffix}` : ''}` : 'Not set') : 'Status unavailable'}</p>
      {identity?.login && <p className="text-sm">Validated as {identity.login}. Scopes: {identity.scopes || 'Not reported by GitHub (fine-grained tokens use repository permissions).'}</p>}
      <Field label="GitHub token"><Input type="password" autoComplete="new-password" value={token} onChange={e => setToken(e.target.value)} disabled={busy} /></Field>
      <div className="flex gap-2"><Button disabled={busy || !token.trim()} onClick={() => setAction('save')}>{state.data?.set ? 'Validate and replace' : 'Validate and save'}</Button>
        {state.data?.set && <Button disabled={busy} onClick={() => setAction('remove')}>Remove token</Button>}</div>
      <ErrorNote error={error || state.error} />
    </div>
    <SecurityVerification open={action !== null} onClose={() => setAction(null)} onVerified={() => void change()} />
  </Surface>;
}
