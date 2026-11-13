import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { UpdateState } from './Updates';
import { QueryState } from './ui';
interface BackupStatus { targets: { id: string; repository: string; enabled: boolean; captured_at?: number; status: string; stale: boolean }[]; installed_local?: { repository: string; last_run: number | null; state: string } }
export function SetupInfrastructureStatus({ kind, owner }: { kind: 'backups' | 'updates'; owner: boolean }) {
  const backups = useQuery({ queryKey: ['setup-backup-status', owner], queryFn: () => api<BackupStatus>('/api/host/backups'), enabled: owner && kind === 'backups', refetchInterval: 15000 });
  const updates = useQuery({ queryKey: ['setup-update-status', owner], queryFn: () => api<UpdateState>('/api/updates'), enabled: owner && kind === 'updates' });
  if (!owner) return null;
  return kind === 'backups' ? <QueryState query={backups}>{data => <div className="space-y-2">
    <p>Targets configured: {data.targets.length}. Recovery kit saved: confirm offline possession yourself; a download alone does not prove it is saved.</p>
    <ul>{data.targets.map(target => <li key={target.id}>{target.repository}: {target.enabled ? 'enabled' : 'disabled'}; successful capture: {target.captured_at ? new Date(target.captured_at * 1000).toLocaleString() : 'not reported'}; {target.status}{target.stale ? ' · stale' : ''}.</li>)}</ul>
    <p>Installer timer (separate same-disk copy): {data.installed_local ? `${data.installed_local.repository} · ${data.installed_local.state} · last run ${data.installed_local.last_run ? new Date(data.installed_local.last_run * 1000).toLocaleString() : 'not reported'}` : 'not reported'}. This is not disk-loss protection.</p>
  </div>}</QueryState> : <QueryState query={updates}>{data => <div className="space-y-2"><p>Repository: {data.repository}. Version: {data.version}. Eligibility: {data.blocked || (data.helper ? 'installer helper available; tagged-release checks still apply' : 'manual update required; installer helper unavailable')}.</p><p>Automatic updates: {data.automatic ? 'configured On' : 'Off'}. Release channel: {data.release_channel}. Setup has not changed this configuration.</p></div>}</QueryState>;
}
