import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { useConfirm } from './confirm';
import { buttonClass, Badge, Card, CardHeader, ErrorNote, QueryState } from './ui';

interface FeatureRow { id: string; label: string; description: string; enabled: boolean; preview: boolean; sensitive: boolean; locked: boolean; qualified?: boolean }
interface FeatureState { revision: number; features: FeatureRow[]; cleanup_pending?: string[] }

// Turning these off stops running work; they are not silently restarted when turned back on.
const STOPS: Record<string, string> = {
  teams: 'Running agent teams are stopped through the normal safe stop path. Turning this back on does not restart them; each team needs a new grant.',
  groups: 'Running agent teams in group projects are stopped. Turning this back on does not restart them.',
  orchestrator: 'Running Orchestrator v2 teams are stopped, and their deliveries and gates go through the normal cancellation. Turning this back on does not restart them.',
  remote_access: 'Public streams close immediately, including your own if you are using the public address. Use the private address to get back in.',
};

const verifyLink = <Link className={buttonClass({ size: "sm" })} to="/settings#passkeys">Verify your sign-in in Passkeys</Link>;

export function AdminFeatures() {
  const client = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({ queryKey: ['admin-features'], queryFn: () => api<FeatureState>('/api/admin-features') });
  const change = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean; label: string }) => api<FeatureState>(`/api/admin-features/${id}`, {
      method: 'PUT', json: { enabled, revision: query.data?.revision },
    }),
    onSuccess: data => { client.setQueryData(['admin-features'], data); client.invalidateQueries({ queryKey: ['auth'] }); client.invalidateQueries({ queryKey: ['containers'] }); },
    onError: () => { void query.refetch(); },
  });
  const stale = change.error instanceof ApiError && change.error.status === 409 && change.error.message.includes('another tab');
  const unverified = change.error instanceof ApiError && change.error.status === 403 && change.error.message.startsWith('Verify your sign-in');
  const toggle = async (row: FeatureRow, enabled: boolean) => {
    if (change.isPending) return;
    if (!enabled && STOPS[row.id] && !await confirm({ title: `Turn off ${row.label}?`, body: STOPS[row.id], confirm: 'Turn off', danger: true })) return;
    change.mutate({ id: row.id, enabled, label: row.label });
  };
  const pending = [...new Set(query.data?.cleanup_pending ?? [])];
  return <Card>
    <CardHeader title="Admin features" subtitle="Owner controls for optional features. Off hides a feature and refuses new work; its saved data is kept." />
    <div className="space-y-4 p-4">
      <p className="text-sm text-muted">A switch set in the server's configuration is locked here; change that configuration to unlock it. Turning a feature on doesn't skip its own qualification, consent or project settings.</p>
      <p className="text-sm text-muted">Features marked <b className="font-medium">Needs verification</b> can only be turned on within five minutes of verifying your sign-in. {verifyLink}, then return here.</p>
      {pending.map(message => <p key={message} role="alert" className="text-sm text-wait">{message}</p>)}
      {stale ? <ErrorNote error="Admin features changed in another tab. The switches below now show the current state; check them and try again." />
        : <ErrorNote error={change.error} title="Couldn't change this feature." action={unverified ? verifyLink : undefined} />}
      {change.isSuccess && <p role="status" className="text-xs text-muted">Saved: {change.variables.label} is {change.variables.enabled ? 'on' : 'off'}.</p>}
      <QueryState query={query}>{data => <>{[false, true].map(preview => <section key={String(preview)} aria-label={preview ? 'Previews' : 'Features'}>
        <h3 className="mb-2 text-sm font-semibold">{preview ? 'Previews' : 'Features'}</h3>
        <ul className="divide-y divide-line">{data.features.filter(row => row.preview === preview).map(row => {
          const saving = change.isPending && change.variables?.id === row.id;
          const verify = row.sensitive && !row.enabled && !row.locked;
          return <li key={row.id} className="flex min-w-0 items-start gap-3 py-3">
            <div className="min-w-0 flex-1 space-y-1 [overflow-wrap:anywhere]">
              <label htmlFor={`feature-${row.id}`} className="text-sm font-medium">{row.label}</label>
              <p id={`feature-help-${row.id}`} className="text-xs text-muted">{row.description}</p>
              <div data-settings-informational id={`feature-status-${row.id}`} className="flex flex-wrap gap-1">
                <Badge tone={row.enabled ? 'ok' : 'neutral'}>{saving ? 'Saving…' : row.enabled ? 'On' : 'Off'}</Badge>
                {row.preview && <Badge>Preview</Badge>}
                {verify && <Badge tone="wait">Needs verification</Badge>}
                {row.id === 'containers' && !row.qualified && <Badge tone="wait">Needs qualification</Badge>}
                {row.locked && <Badge>Locked</Badge>}
              </div>
              {row.locked && <p id={`feature-locked-${row.id}`} className="text-xs text-muted">Set by the server's configuration. Change it there to unlock this switch.</p>}
            </div>
            <input id={`feature-${row.id}`} type="checkbox" role="switch" checked={saving ? change.variables.enabled : row.enabled}
              disabled={row.locked} aria-disabled={change.isPending || undefined} aria-busy={saving || undefined}
              aria-describedby={[`feature-help-${row.id}`, `feature-status-${row.id}`, row.locked && `feature-locked-${row.id}`].filter(Boolean).join(' ')}
              className="mt-1 size-5 shrink-0 accent-[var(--accent)] pointer-coarse:size-6 aria-disabled:opacity-60"
              onChange={event => void toggle(row, event.target.checked)} />
          </li>;
        })}</ul>
      </section>)}</>}</QueryState>
    </div>
  </Card>;
}
