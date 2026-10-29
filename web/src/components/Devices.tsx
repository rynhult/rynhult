import { Feature } from "./Feature";
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Laptop } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, post } from '../lib/api';
import { OFFLINE_TITLE, useOffline } from '../lib/queries';
import { useConfirm } from './confirm';
import { announce, Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, Input, LoadingRows, QueryState } from './ui';

interface Device { id: string; person: string; label: string; kind: string; status: string; expires_at: string; last_seen_at: string | null; previous_authority?: boolean }
interface State { devices: Device[]; pairing_enabled: boolean; policy_pending: boolean }
interface Pairing { id: string; label: string; link: string; qr_svg: string; expires_at: string }
interface Detail { status: string; password: string | null }

/** The owner types the code read from the receiving device: Rynhult never shows it here, and one wrong entry cancels. */
export function ConfirmByCode({ id, label, person, onConfirmed, onError }: { id: string; label: string; person?: string; onConfirmed: () => Promise<unknown>; onError?: (error: unknown) => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const offline = useOffline();
  const confirm = useConfirm();
  const device = person ? `${person}’s ${label}` : label;
  return <Feature name="remote_access"><form className="flex flex-wrap items-end gap-3" onSubmit={async e => {
    e.preventDefault();
    if (!await confirm({ title: 'Confirm device?', body: `Issue a browser certificate for ${device}? Only continue if you read code ${code} on that device yourself. A wrong code cancels this pairing.`, confirm: 'Confirm device' })) return;
    setBusy(true); setError(null); onError?.(null);
    try { await post(`/api/devices/pairings/${id}/confirm`, { matching_code: code }); setCode(''); await onConfirmed(); announce('Device confirmed. Show the import password on the trusted device.'); }
    catch (e) { if (onError) onError(e); else setError(e); await onConfirmed(); } finally { setBusy(false); }
  }}>
    <Field label={`Matching code shown on ${label}`}><Input name="matching-code" inputMode="numeric" autoComplete="off" maxLength={6} pattern="[0-9]{6}" required value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} placeholder="000000…" /></Field>
    <Button type="submit" aria-label={`Confirm ${label}`} disabled={busy || offline || code.length !== 6} title={offline ? OFFLINE_TITLE : undefined}>Confirm</Button>
    <ErrorNote error={error} />
  </form></Feature>;
}

export function PairingDialog({ pairing, close, owner }: { pairing: Pairing | null; close: () => void; owner: boolean }) {
  // Secret-bearing responses bypass the shared query cache.
  const [detail, setDetail] = useState<Detail | null>(null);
  const [pollError, setPollError] = useState<unknown>(null);
  const offline = useOffline();
  const [showPassword, setShowPassword] = useState(false);
  const client = useQueryClient();
  useEffect(() => {
    if (!pairing) return;
    let closed = false;
    const poll = () => { void api<Detail>(`/api/devices/pairings/${pairing.id}`).then(d => { if (!closed) { setDetail(d); setPollError(null); } }).catch(e => { if (!closed) { setDetail(null); setPollError(e); } }); };
    const timer = setInterval(poll, 3000);
    return () => { closed = true; clearInterval(timer); };
  }, [pairing]);
  const [error, setError] = useState<unknown>(null);
  // Closing ends a link nobody confirmed yet, and so does leaving the page with Back or a link while it is open; the
  // server keeps an import the owner already confirmed (break-it 10).
  const status = useRef<string | undefined>(undefined);
  status.current = detail?.status;
  useEffect(() => {
    if (!pairing) return;
    return () => {
      if (!status.current || status.current === 'pending' || status.current === 'claimed') void post(`/api/devices/pairings/${pairing.id}/cancel`).catch(() => undefined);
    };
  }, [pairing]);
  return <Dialog open={!!pairing} onClose={close} title="Pair device" description="Open this one-use link on the new device while it is on your private LAN. The link expires in five minutes."
    footer={<Button onClick={close}>Close pairing</Button>}>
    {pairing && <div className="space-y-4">
      <img alt="One-use device pairing QR code" width={240} height={240} src={`data:image/svg+xml,${encodeURIComponent(pairing.qr_svg)}`} />
      <Field label="One-use pairing link"><Input readOnly value={pairing.link} autoComplete="off" spellCheck={false} /></Field>
      <p className="text-sm text-muted">Keep this link private. On the new device select Request pairing, then ask an owner to confirm the device in Remote access.</p>
      {detail?.status === 'claimed' && <p className="text-sm text-fg">The new device shows a six-digit matching code. {owner ? 'Type it below from that screen.' : 'Read it to an owner from that screen.'} If it shows an error instead, close this pairing.</p>}
      {owner && detail?.status === 'claimed' && <ConfirmByCode id={pairing.id} label={pairing.label} onError={setError} onConfirmed={() => client.invalidateQueries({ queryKey: ['devices'] })} />}
      <Button disabled={offline} title={offline ? OFFLINE_TITLE : undefined} onClick={async () => { setShowPassword(true); try { setDetail(await api<Detail>(`/api/devices/pairings/${pairing.id}`)); setError(null); setPollError(null); } catch (e) { setDetail(null); setError(e); } }}>Show import password</Button>
      {showPassword && detail && <p className="text-sm text-muted">{detail.password ? <>Import password: <code className="break-all text-fg">{detail.password}</code>. Read this to the new device separately; it expires with the download.</> : `Pairing: ${detail.status}. An owner must confirm before a password is available.`}</p>}
      <ErrorNote error={error || pollError} />
    </div>}
  </Dialog>;
}

export function Devices({ owner, registry = false }: { owner: boolean; registry?: boolean }) {
  const client = useQueryClient();
  const confirm = useConfirm();
  const offline = useOffline();
  const query = useQuery({ queryKey: ['devices', registry ? 'registry' : 'mine'], queryFn: () => api<State>(registry ? '/api/devices' : '/api/devices?mine=true') });
  const [name, setName] = useState('');
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [rename, setRename] = useState<Device | null>(null);
  const [label, setLabel] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await client.invalidateQueries({ queryKey: ['devices'] }); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  const blocked = offline || busy;
  const daysLeft = (d: Device) => Math.ceil((new Date(d.expires_at).getTime() - Date.now()) / 86400000);
  return <Card>
    <CardHeader title={registry ? 'Device registry' : 'Access devices'} icon={Laptop} subtitle={registry ? 'Paired browser identities for everyone; enabled public policies reconcile automatically' : 'Browser identities that control access to Rynhult'} />
    <div className="space-y-4 p-4">
      <p className="text-sm text-muted">Pairing prepares a browser certificate; owners manage public access in Remote access. Verify your sign-in under Passkeys before pairing, renewing or revoking. <Link to="/guide/User-Guide/Remote-Access">Read the Guide</Link></p>
      <QueryState query={query} loading={<LoadingRows rows={2} />}>{data => <>
        <Feature name="remote_access"><form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); void run(async () => { setPairing(await post<Pairing>('/api/devices/pairings', { label: name })); announce('Pairing link created.'); }); }}>
          <Field label="Device name"><Input name="device-name" autoComplete="off" value={name} onChange={e => setName(e.target.value)} maxLength={100} required /></Field>
          <Button type="submit" disabled={blocked || !data.pairing_enabled} title={offline ? OFFLINE_TITLE : !data.pairing_enabled ? 'An owner must allow LAN pairing in Remote access.' : undefined}>Pair device</Button>
        </form></Feature>
        {!data.devices.length ? <Empty icon={Laptop}>No paired devices yet.</Empty> : <ul className="divide-y divide-line">{data.devices.map(d => <li key={d.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="min-w-0"><p className="break-words text-sm text-fg">{d.label} <Badge>{d.status === 'revoked' && data.policy_pending ? 'Revocation pending' : d.status}</Badge></p>
            {d.status === 'active' && d.previous_authority && <p className="text-xs text-wait">Issued by the previous device authority: renew it on the LAN before the owner finishes the rollover.</p>}
            {d.status === 'active' && daysLeft(d) <= 30 && <p className="text-xs text-wait">{daysLeft(d) <= 7 ? '7-day' : daysLeft(d) <= 14 ? '14-day' : '30-day'} renewal reminder: expires in {daysLeft(d)} days. Renew manually on the LAN.</p>}
            <p className="break-words text-xs text-muted">{owner ? `${d.person} · ` : ''}Expires {new Date(d.expires_at).toLocaleDateString()} · Last connection: {d.last_seen_at ? new Date(d.last_seen_at).toLocaleString() : 'not observed'}</p></div>
          <div className="flex flex-wrap gap-2">
            <Button aria-label={`Rename ${d.label}`} disabled={blocked} onClick={() => { setRename(d); setLabel(d.label); }}>Rename</Button>
            {d.status === 'active' && <>{!registry && <Button aria-label={`Renew ${d.label}`} disabled={blocked} onClick={() => void run(async () => setPairing(await post<Pairing>(`/api/devices/${d.id}/renew`)))}>Renew</Button>}
              <Button aria-label={`Revoke ${d.label}`} variant="danger" disabled={blocked} onClick={async () => { if (await confirm({ title: `Revoke ${d.label}?`, body: 'This ends every sign-in and push subscription for this person and closes their terminals. If public access is enabled, Rynhult re-applies the device allow list automatically. Check Remote access for the verified result or a pending failure. Remove the imported identity from the device too.', confirm: 'Revoke device', danger: true })) void run(() => post(`/api/devices/${d.id}/revoke`)); }}>Revoke</Button></>}
            <Button aria-label={`Remove ${d.label}`} variant="danger" disabled={blocked} onClick={async () => {
              if (await confirm({ title: `Remove ${d.label}?`, body: 'Revoke this identity and remove its registry record? This ends every sign-in and push subscription for this person, closes their terminals, and updates the enabled public policy. Remove the local imported identity too.', confirm: 'Remove device', danger: true })) {
                void run(() => api(`/api/devices/${d.id}`, { method: 'DELETE' }));
              }
            }}>Remove</Button>
          </div>
        </li>)}</ul>}
      </>}</QueryState>
      <ErrorNote error={error} />
      {!registry && <p className="text-xs text-muted">Alerts are listed under <a href="#notification-devices" className="text-accent underline underline-offset-2">Notification devices</a>. Revoking a device here also ends your notification subscriptions.</p>}
    </div>
    {pairing && <Feature name="remote_access"><PairingDialog key={pairing.id} pairing={pairing} owner={owner} close={() => setPairing(null)} /></Feature>}
    <Dialog open={!!rename} onClose={() => setRename(null)} title="Rename device" dismissible={false}>
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); void run(async () => { await api(`/api/devices/${rename!.id}`, { method: 'PATCH', json: { label } }); setRename(null); announce('Device renamed.'); }); }}>
        <Field label="New device name"><Input name="device-label" autoComplete="off" value={label} maxLength={100} required onChange={e => setLabel(e.target.value)} /></Field>
        <Button type="submit" disabled={blocked}>Save name</Button><ErrorNote error={error} />
      </form>
    </Dialog>
  </Card>;
}
