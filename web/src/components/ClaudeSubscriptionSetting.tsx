import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { Surface as Card, SurfaceHeader as CardHeader } from './shell/PagePrimitives';
import { Button } from './ui';

type Consent = { enabled: boolean; available: boolean; notice: string; risk: string; current_version: string };
export function ClaudeSubscriptionSetting() {
  const cache = useQueryClient();
  const { data, error } = useQuery({ queryKey: ['claude-subscription'], queryFn: () => api<Consent>('/api/preferences/claude-subscription') });
  const [accepted, setAccepted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState('');
  async function save(enabled: boolean) {
    setSaving(true); setFailure('');
    try {
      await api('/api/preferences/claude-subscription', { method: 'PUT', json: { enabled, version: data?.current_version } });
      setAccepted(false);
      await cache.invalidateQueries({ queryKey: ['claude-subscription'] });
    } catch (e) { setFailure(String(e)); }
    finally { setSaving(false); }
  }
  return <Card><CardHeader title="Claude Agent" />
    {error && <p role="alert">{String(error)}</p>}
    {data && <>
      <p>Use my Claude subscription for Chat</p>
      <blockquote>{data.notice}</blockquote>
      <p>{data.risk}</p>
      <p>API key and Bedrock, Vertex or Foundry authentication remain available. The session relay must also be enabled and qualified.</p>
      {!data.available && <p>The owner has disabled subscription Chat.</p>}
      {!data.enabled && <label className="flex min-h-11 items-center gap-2"><input type="checkbox" className="h-5 w-5" checked={accepted} onChange={e => setAccepted(e.target.checked)} /> I accept this account risk.</label>}
      <Button disabled={saving || (!data.enabled && (!accepted || !data.available))} onClick={() => void save(!data.enabled)}>{data.enabled ? 'Revoke subscription Chat' : 'Enable subscription Chat'}</Button>
    </>}
    {failure && <p role="alert">{failure}</p>}
  </Card>;
}
