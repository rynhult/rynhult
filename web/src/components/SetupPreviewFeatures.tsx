import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { useConfirm } from './confirm';
import { Button, ErrorNote, QueryState } from './ui';
interface Row { id: string; label: string; description: string; enabled: boolean; preview: boolean; locked: boolean; sensitive: boolean; qualified?: boolean }
interface Registry { revision: number; features: Row[] }
export function SetupPreviewFeatures({ owner }: { owner: boolean }) {
  const client = useQueryClient(); const confirm = useConfirm();
  const query = useQuery({ queryKey: ['setup-features', owner], queryFn: () => api<Registry>('/api/admin-features'), enabled: owner, refetchOnMount: 'always' });
  const [picked, setPicked] = useState<string[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  if (!owner) return <p>An owner can choose installation preview features. Your personal setup can continue.</p>;
  return <div className="space-y-3">
    <p>No changes are selected. Enabling availability still requires separate configuration, qualification and consent. It does not enroll devices, expose public access or provision enterprise identity.</p>
    <Link className="underline" to="/settings#passkeys">Verify sign-in for sensitive changes, then return</Link>
    <QueryState query={query}>{data => <ul className="space-y-3">{data.features.filter(row => row.preview && row.id !== 'new_shell').map(row => <li key={row.id}>
      <label className="flex min-h-11 items-start gap-3"><input className="mt-1 size-5 shrink-0" type="checkbox" disabled={row.locked || busy} checked={picked.includes(row.id)} onChange={e => setPicked(e.target.checked ? [...picked, row.id] : picked.filter(id => id !== row.id))} />
        <span><strong>{row.label}</strong> · currently {row.enabled ? 'On' : 'Off'} → {row.enabled ? 'Off' : 'On'}<br />{row.description}
          {row.locked && <span className="block">Locked by server configuration.</span>}{row.sensitive && <span className="block">Requires fresh sign-in verification.</span>}{row.qualified === false && <span className="block">Needs qualification before use.</span>}</span>
      </label>
    </li>)}</ul>}</QueryState>
    <Button disabled={busy || !picked.length || !query.data} onClick={async () => {
      setBusy(true); setError(null);
      try {
        // A fresh registry invalidates the draft; each change has its own confirmation.
        let live = await api<Registry>('/api/admin-features');
        if (live.revision !== query.data?.revision) { setPicked([]); throw new Error('Features changed. Review the refreshed choices and select again.'); }
        for (const id of picked) {
          const row = live.features.find(item => item.id === id);
          if (!row || row.locked) throw new Error('This choice is unavailable or locked. Review again.');
          if (!await confirm({ title: `${row.enabled ? 'Turn off' : 'Enable'} ${row.label}?`, body: `${row.description} Separate configuration, qualification and consent still apply.${row.enabled && id === 'remote_access' ? ' Public streams close immediately; use your private address to return.' : ''}`, confirm: 'Confirm this change' })) break;
          live = await api<Registry>(`/api/admin-features/${id}`, { method: 'PUT', json: { enabled: !row.enabled, revision: live.revision } });
          setPicked(previous => previous.filter(item => item !== id));
          await client.invalidateQueries({ queryKey: ['auth'] });
        }
      } catch (err) { setError(err); }
      finally { setBusy(false); await query.refetch(); }
    }}>Review selected feature changes</Button>
    <ErrorNote error={error} />
  </div>;
}
