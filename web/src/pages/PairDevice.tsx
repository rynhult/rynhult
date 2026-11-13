import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, downloadFile, post } from '../lib/api';
import { useAppliedTheme } from '../lib/theme';
import { announce, Button, Card, CardHeader, ErrorNote, Page, PageHeader, Field, Select } from '../components/ui';

function terminalPairingError(error: unknown) {
  return error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429;
}

export function PairDevice() {
  useAppliedTheme();
  const lifecycle = useRef(0);
  const [token, setToken] = useState(() => window.location.hash.slice(1));
  const [preview, setPreview] = useState<{ installation: string; installation_name: string; person: string; label: string; desktop_code: string } | null>(null);
  const [unusable, setUnusable] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [receipt, setReceipt] = useState('');
  const [matchingCode, setMatchingCode] = useState('');
  const [compared, setCompared] = useState(false);
  const [available, setAvailable] = useState(false);
  const [format, setFormat] = useState('pkcs12');
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!token) return;
    let closed = false;
    const generation = lifecycle.current;
    setPreviewLoading(true); setError(null); setCompared(false);
    void post<{ installation: string; installation_name: string; person: string; label: string; desktop_code: string }>('/api/device-pairing/preview', { token })
      .then(value => { if (!closed && generation === lifecycle.current) setPreview(value); }).catch(e => {
        if (closed || generation !== lifecycle.current) return;
        setError(e);
        if (terminalPairingError(e)) { setToken(''); setUnusable(true); }
      }).finally(() => { if (!closed && generation === lifecycle.current) setPreviewLoading(false); });
    return () => { closed = true; };
  }, [token, previewAttempt]);
  useEffect(() => { window.history.replaceState(null, '', '/pair-device'); }, []);
  useEffect(() => {
    // A restored browser-history page must not retain either one-use capability.
    const clear = () => { lifecycle.current++; setBusy(false); setError(null); setUnusable(false); setToken(''); setReceipt(''); setMatchingCode(''); setPreview(null); setCompared(false); setAvailable(false); setDone(false); };
    window.addEventListener('pagehide', clear);
    return () => { lifecycle.current++; window.removeEventListener('pagehide', clear); };
  }, []);
  useEffect(() => { if (available) announce('Owner confirmed. The identity is ready to download once.'); }, [available]);
  useEffect(() => {
    if (!receipt || done) return;
    let closed = false;
    const generation = lifecycle.current;
    const poll = async () => {
      try {
        const result = await post<{ available: boolean }>('/api/device-pairing/status', { receipt });
        if (closed || generation !== lifecycle.current) return;
        setAvailable(result.available); setError(null);
      } catch (e) {
        if (closed || generation !== lifecycle.current) return;
        setAvailable(false); setError(e);
        if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429) return;
      }
      if (!closed && generation === lifecycle.current) timer = setTimeout(poll, 3000);
    };
    let timer = setTimeout(poll, 3000);
    return () => { closed = true; clearTimeout(timer); };
  }, [receipt, done]);
  return <Page><PageHeader title="Pair this device" description="Use the private LAN address. Keep the trusted device nearby." />
    <Card><CardHeader title="Browser identity" subtitle="One-use import · expires within five minutes of owner confirmation" />
      <div className="space-y-4 p-4">
        {unusable ? <p className="text-sm text-muted">This pairing is no longer available. Open a fresh QR code from your trusted device.</p> : !token && !receipt && !done ? <p className="text-sm text-muted">Open a fresh pairing link from your trusted device. A refreshed page cannot recover a one-use capability.</p> : !receipt && !done ? <>
          <p className="text-sm text-muted">Request pairing, then ask the owner to confirm your device. Rynhult will provide an encrypted certificate to import in your operating system or browser.</p>
          {preview && <p className="text-sm text-fg">Installation: {preview.installation_name} ({preview.installation}) · Person: {preview.person} · Device: {preview.label}. Desktop match code: <code>{preview.desktop_code}</code>. Compare it with your trusted desktop. Only request pairing if all details match.</p>}
          {preview && <label className="flex items-center gap-2 text-sm text-fg"><input type="checkbox" checked={compared} onChange={e => setCompared(e.target.checked)} />The installation, person and desktop match code match my trusted screen</label>}
          {!preview && (previewLoading ? <p className="text-sm text-muted">Checking pairing details…</p> : <Button onClick={() => setPreviewAttempt(attempt => attempt + 1)}>Retry pairing preview</Button>)}
          {preview && <Button disabled={busy || !compared} onClick={async () => { const generation = lifecycle.current; setBusy(true); setError(null); try { const result = await post<{ receipt: string; matching_code: string }>('/api/device-pairing/claim', { token, phone_confirmed: compared }); if (generation !== lifecycle.current) return; setReceipt(result.receipt); setToken(''); setPreview(null); setCompared(false); setMatchingCode(result.matching_code); announce('Waiting for owner confirmation.'); } catch (e) { if (generation !== lifecycle.current) return; setError(e); if (terminalPairingError(e)) { setToken(''); setPreview(null); setCompared(false); setUnusable(true); } } finally { if (generation === lifecycle.current) setBusy(false); } }}>Request pairing</Button>}
        </> : done ? <>
          <p className="text-sm text-muted">Certificate downloaded once. Import the downloaded identity using the password shown separately on the trusted device. Rynhult cannot detect whether the import succeeded. Use the public address only after an owner has applied and qualified remote access.</p>
          <Link to="/login">Continue to sign in</Link>
        </> : <>
          {!available && <p className="text-sm text-fg">Matching code: <code>{matchingCode}</code>. Ask the owner to compare this code on their trusted device. If it differs, stop and start a new pairing.</p>}
          <p className="text-sm text-muted">{available ? 'Confirmed. Ask for the separate import password before downloading.' : 'Waiting for owner confirmation.'}</p>
          <p className="text-sm text-muted">On Android import the .p12 in credential settings. On iPhone or iPad use the identity import supported by your OS; physical-device compatibility is still being qualified. This is separate from Rynhult’s server trust certificate.</p>
          <Field label="Identity format"><Select value={format} onChange={e => setFormat(e.target.value)}><option value="pkcs12">PKCS#12 (.p12): Android and desktop</option><option value="mobileconfig">Apple identity profile: iPhone, iPad and Mac</option></Select></Field>
          <Button disabled={busy || !available} onClick={async () => {
            setBusy(true); setError(null);
            try { await downloadFile('/api/device-pairing/download', `acc-device.${format === 'mobileconfig' ? 'mobileconfig' : 'p12'}`, { receipt, format }); setDone(true); setReceipt(''); announce('Certificate downloaded once.'); }
            catch (e) { setError(e); } finally { setBusy(false); }
          }}>Download identity once</Button>
        </>}
        <ErrorNote error={error} />
      </div>
    </Card>
  </Page>;
}
