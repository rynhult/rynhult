import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Shield } from 'lucide-react';
import { api, post } from '../lib/api';
import { OFFLINE_TITLE, useOffline } from '../lib/queries';
import { useConfirm } from '../components/confirm';
import { ConfirmByCode, Devices } from '../components/Devices';
import { announce, Badge, Button, Card, CardHeader, ErrorNote, Field, Input, LoadingRows, Note, Page, PageHeader, QueryState } from '../components/ui';

interface Settings { pairing_enabled: boolean; public_hostname: string; private_origin: string; certificate_days: number; machine_identities: boolean; settings_revision: string }
interface Pending { id: string; person: string; label: string; status: string }
interface PublicState { desired_enabled: boolean; enabled: boolean | null; hostname: string; last_applied_revision: string | null; devices_allowed: number; last_reload_result: string; pending: boolean;
  rollover?: { authority: string | null; previous_identities: number } }
interface Review { revision: string; configuration: unknown; devices_allowed: number }

function Preparation({ initial }: { initial: Settings }) {
  const client = useQueryClient();
  const { settings_revision, ...values } = initial;
  const [draft, setDraft] = useState(values);
  const [savedDraft, setSavedDraft] = useState(JSON.stringify(values));
  const [revision, setRevision] = useState(settings_revision);
  const dirty = JSON.stringify(draft) !== savedDraft;
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [policy, setPolicy] = useState('');
  const offline = useOffline();
  return <div className="space-y-4 p-4">
    <p className="text-sm text-muted">Saving pairing settings does not enable public access. Review and Apply are separate actions and require the separately installed, qualified helper. Use the private recovery address; remote previews stay private.</p>
    <p className="text-sm text-muted">First verify your sign-in in <Link to="/settings">Settings → Passkeys</Link>. Certificates last up to 90 days and need manual renewal and another import.</p>
    <form className="grid gap-3 sm:grid-cols-2" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError(null); setSaved(false);
      try { const result = await api<Settings>('/api/remote-access', { method: 'PUT', json: { ...draft, expected_revision: revision } }); setRevision(result.settings_revision); await client.invalidateQueries({ queryKey: ['devices'] }); await client.invalidateQueries({ queryKey: ['remote-access'] }); setPolicy(''); setSavedDraft(JSON.stringify(draft)); setSaved(true); announce('Pairing settings saved.'); }
      catch (e) { setError(e); } finally { setBusy(false); }
    }}>
      <Field label="Public hostname"><Input name="public-hostname" autoComplete="off" spellCheck={false} value={draft.public_hostname} onChange={e => setDraft({ ...draft, public_hostname: e.target.value })} placeholder="remote.example.com…" /></Field>
      <Field label="Private recovery address"><Input name="private-origin" autoComplete="off" type="url" spellCheck={false} required value={draft.private_origin} onChange={e => setDraft({ ...draft, private_origin: e.target.value })} placeholder="https://rynhult.example.lan…" /></Field>
      <Field label="Certificate lifetime (days)"><Input name="certificate-days" type="number" min={1} max={90} required value={draft.certificate_days} onChange={e => setDraft({ ...draft, certificate_days: Number(e.target.value) })} /></Field>
      <label className="flex items-center gap-2 text-sm text-fg"><input type="checkbox" checked={draft.pairing_enabled} onChange={e => setDraft({ ...draft, pairing_enabled: e.target.checked })} /> Allow LAN device pairing</label>
      <label className="flex items-center gap-2 text-sm text-fg sm:col-span-2"><input type="checkbox" name="machine-identities" checked={draft.machine_identities} onChange={e => setDraft({ ...draft, machine_identities: e.target.checked })} /> Give enrolled machines identities (30 days, rotated automatically over their link)</label>
      <Button type="submit" disabled={offline || busy} title={offline ? OFFLINE_TITLE : undefined}>{busy ? 'Saving…' : 'Save pairing settings'}</Button>
    </form>
    {saved && <p className="text-sm text-muted">Pairing settings saved. Public routing was not changed.</p>}
    <Button disabled={offline || busy} title={offline ? OFFLINE_TITLE : undefined} onClick={async () => { setError(null); try { const result = await api<{ fragment: unknown }>('/api/remote-access/policy'); setPolicy(JSON.stringify(result.fragment, null, 2)); } catch (e) { setError(e); } }}>Render Caddy fragment</Button>
    {policy && <pre className="max-h-72 overflow-auto text-xs text-muted" tabIndex={0} role="region" aria-label="Unapplied Caddy fragment">{policy}</pre>}
    <ErrorNote error={error} />
  </div>;
}

export function RemoteAccess() {
  const client = useQueryClient();
  const [pairingError, setPairingError] = useState<unknown>(null);
  const query = useQuery({ queryKey: ['remote-access'], queryFn: () => api<Settings & PublicState>('/api/remote-access'), refetchInterval: 5000 });
  const pending = useQuery({ queryKey: ['device-pairings'], queryFn: () => api<{ pairings: Pending[] }>('/api/devices/pairings'), refetchInterval: 3000 });
  return <Page>
    <PageHeader title="Remote access" description="Away from your network, reach Rynhult through your VPN or Tailscale: that needs nothing here. This page is the optional public hostname without a VPN (off by default, not yet qualified for v1) and paired devices. Keep your private recovery address available." actions={<Link to="/guide/User-Guide/Remote-Access">Guide</Link>} />
    <PublicAccess />
    <Card><CardHeader title="Pairing settings" icon={Shield} subtitle="LAN preparation · saving does not enable public access" />
      <QueryState query={query} loading={<LoadingRows rows={3} />}>{data => <Preparation initial={{ pairing_enabled: data.pairing_enabled, public_hostname: data.public_hostname, private_origin: data.private_origin, certificate_days: data.certificate_days, machine_identities: data.machine_identities, settings_revision: data.settings_revision }} />}</QueryState>
    </Card>
    <Card><CardHeader title="Confirm pairing" icon={Shield} subtitle="Only confirm a request you recognize from the person and their new device" />
      <div className="space-y-3 p-4"><p className="text-sm text-muted">Read the six-digit matching code on the new device yourself and type it here. Rynhult does not show it on this page; a wrong code cancels the pairing.</p>
        <QueryState query={pending} loading={<LoadingRows rows={1} />}>{data => data.pairings.length ? data.pairings.map(p => <div className="space-y-2" key={p.id}>
        <p className="min-w-0 break-words text-sm text-fg">{p.label} · {p.person}</p>
        <ConfirmByCode id={p.id} label={p.label} person={p.person} onError={setPairingError} onConfirmed={async () => { await pending.refetch(); await client.invalidateQueries({ queryKey: ['devices'] }); }} />
      </div>) : <p className="text-sm text-muted">No devices waiting for confirmation.</p>}</QueryState>
        <ErrorNote error={pairingError} />
      </div>
    </Card>
    <Devices owner registry />
  </Page>;
}


function PublicAccess() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['remote-access'], queryFn: () => api<Settings & PublicState>('/api/remote-access'), refetchInterval: 5000 });
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const offline = useOffline();
  const confirm = useConfirm();
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await work(); } catch (e) { setError(e); }
    finally { await client.invalidateQueries({ queryKey: ['remote-access'] }); setBusy(false); }
  };
  // The server checks the revision again against current settings, registry and root template.
  return <Card><CardHeader title="Public access status" icon={Shield} subtitle="Last helper acknowledgement · refreshes every five seconds" />
    <div className="space-y-4 p-4">
      <QueryState query={query} loading={<LoadingRows rows={3} />}>{data => <>
        <p className="text-sm text-fg">Public access <Badge>{data.enabled === null ? 'Unknown' : data.enabled ? 'On' : 'Off'}</Badge> {data.pending && <Badge>{data.desired_enabled ? 'Apply pending' : 'Removal pending'}</Badge>}</p>
        <dl className="grid gap-2 text-sm sm:grid-cols-2">
          <div><dt className="text-muted">Hostname</dt><dd className="break-all text-fg">{data.hostname || 'None applied'}</dd></div>
          <div><dt className="text-muted">Devices allowed</dt><dd className="text-fg">{data.devices_allowed} at last verified apply</dd></div>
          <div><dt className="text-muted">Last applied revision</dt><dd className="break-all font-mono text-xs text-fg">{data.last_applied_revision || 'Never applied'}</dd></div>
          <div><dt className="text-muted">Last reload result</dt><dd className="text-fg">{data.last_reload_result}</dd></div>
          {data.rollover?.authority && <div><dt className="text-muted">Device authority</dt><dd className="break-all font-mono text-xs text-fg">{data.rollover.authority.slice(0, 16)}…</dd></div>}
        </dl>
        {!!data.rollover?.previous_identities && <Note>{data.rollover.previous_identities} {data.rollover.previous_identities === 1 ? 'identity still comes' : 'identities still come'} from the previous device authority. Machines replace theirs over their link; people renew their devices on the LAN. Finish the rollover only when this reaches zero.</Note>}
      </>}</QueryState>
      <p className="text-sm text-muted">Off by default. Apply requires recent sign-in verification on the private LAN and a qualified root helper. Remote previews stay private. Physical-device and stream-revocation qualification remain required before public use.</p>
      <div className="flex flex-wrap gap-2">
        <Button disabled={offline || busy} title={offline ? OFFLINE_TITLE : undefined} onClick={() => void run(async () => {
          setReview(null); setReview(await api<Review>('/api/remote-access/review')); announce('Caddy configuration ready for review.');
        })}>Review Caddy configuration</Button>
        <Button variant="danger" disabled={offline || busy} title={offline ? OFFLINE_TITLE : undefined} onClick={() => void run(async () => {
          await post('/api/remote-access/off'); setReview(null); announce('Public routing removed. Private recovery is available.');
        })}>Turn off public access</Button>
      </div>
      {review && <>
        <p className="break-all text-sm text-muted">Review revision <code>{review.revision}</code> · {review.devices_allowed} devices. This is what Apply adds to the root-reviewed base (named by its base revision); the base itself, with other sites and DNS credentials, stays with the root helper. The helper validates, writes, reloads and verifies the complete result. Save settings first; any later change requires a new review.</p>
        <pre className="max-h-72 overflow-auto text-xs text-muted" tabIndex={0} role="region" aria-label="Caddy configuration to apply">{JSON.stringify(review.configuration, null, 2)}</pre>
        <Button disabled={offline || busy} title={offline ? OFFLINE_TITLE : undefined} onClick={async () => {
          if (!await confirm({ title: 'Apply public access?', body: 'Enable the reviewed public hostname with the device certificate gate? Keep private recovery open. Only proceed after the helper and release are qualified.', confirm: 'Apply public access' })) return;
          void run(async () => { await post('/api/remote-access/apply', { revision: review.revision }); setReview(null); announce('Public access configuration verified.'); });
        }}>Apply reviewed configuration</Button>
      </>}
      <ErrorNote error={error} />
    </div>
  </Card>;
}
