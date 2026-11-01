import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { stamp } from '../lib/format';
import { Button, Dialog, ErrorNote, Field, Input } from './ui';
type Preview = { revision: string; sessions: { id: string; name: string; provider: string; permissions: Record<string, string | null>; dangerous_permissions: boolean; pending_stop: boolean }[] };
export function TeamLifetime({ id, expiresAt, stopped, reason, available }: { id: string; expiresAt: string | null; stopped: boolean; reason?: string | null; available?: boolean }) {
  const client = useQueryClient();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [hours, setHours] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    if (!preview?.sessions.some(s => s.pending_stop)) return;
    let active = true;
    const timer = window.setInterval(() => {
      void api<Preview>(`/api/sessions/${id}/team/resume`).then(value => { if (active) setPreview(value); }).catch(e => { if (active) setError(e); });
    }, 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [id, preview]);
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(undefined);
    try { await action(); await client.invalidateQueries({ queryKey: ['team', id] }); await client.invalidateQueries({ queryKey: ['attention'] }); await client.invalidateQueries({ queryKey: ['sessions'] }); }
    catch (e) { setError(e); } finally { setBusy(false); }
  }
  return <section aria-label="Team lifetime" className="min-w-0 space-y-2 rounded-lg border border-line p-3">
    <p className="break-words font-medium">{stopped ? reason === 'expired' ? 'This team stopped because its time limit ended.' : 'This team is stopped.' : expiresAt ? `This team stops at ${stamp(expiresAt)}` : 'No time limit for this team.'}</p>
    <div className="flex flex-wrap gap-2">
      {expiresAt && <><Button disabled={busy} onClick={() => void run(async () => { await api(`/api/sessions/${id}/team/lifetime`, { json: { hours: 4 } }); })}>Extend 4 h</Button>
      <Button disabled={busy} onClick={() => void run(async () => { await api(`/api/sessions/${id}/team/lifetime`, { json: { hours: null } }); })}>Remove limit</Button></>}
      {(stopped || available) && <Button disabled={busy} onClick={() => void run(async () => { setPreview(await api<Preview>(`/api/sessions/${id}/team/resume`)); setHours(''); })}>Resume team</Button>}
    </div>
    <ErrorNote error={error} />
    <Dialog open={!!preview} onClose={() => { if (!busy) setPreview(null); }} title="Resume this team?" dismissible={false} footer={<>
      <Button disabled={busy} onClick={() => setPreview(null)}>Cancel</Button>
      <Button variant="primary" disabled={busy || !preview?.sessions.length || preview.sessions.some(s => s.pending_stop)} onClick={() => void run(async () => {
        const result = await api<{ errors: Record<string, string> }>(`/api/sessions/${id}/team/resume`, { json: { revision: preview!.revision, hours: hours === '' ? null : Number(hours) } });
        if (Object.keys(result.errors).length) { setPreview(await api<Preview>(`/api/sessions/${id}/team/resume`)); throw new Error(Object.values(result.errors).join('; ')); }
        setPreview(null);
      })}>Confirm resume</Button>
    </>}>
      <div className="space-y-3">
        <p>Resume these saved sessions with their existing permissions and assignments. Finished, failed and previously stopped helpers stay stopped. Budgets and run mode still apply.</p>
        <Field label="Hours after resume" hint="Empty means no time limit. All other team limits stay unchanged."><Input type="number" min="0.01" max="168" step="any" value={hours} onChange={e => setHours(e.target.value)} /></Field>
        <ul className="space-y-2 text-sm">{preview?.sessions.map(s => <li key={s.id} className="break-words">{s.name} · {s.provider} · {Object.entries(s.permissions).filter(([, value]) => value).map(([key, value]) => `${key}: ${value}`).join(' · ') || 'Provider defaults (saved)'}{s.dangerous_permissions && <span className="block text-wait">Saved permissions allow actions without asking. Resuming keeps these permissions.</span>}{s.pending_stop ? ' · Waiting for stop confirmation' : ''}</li>)}</ul>
        {preview && !preview.sessions.length && <p>No eligible stopped sessions. They may already be running or no longer own their assignments.</p>}
        <ErrorNote error={error} />
      </div>
    </Dialog>
  </section>;
}
