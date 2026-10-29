import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, downloadFile, post } from '../lib/api';
import { useAppliedTheme } from '../lib/theme';
import { announce, Button, Card, CardHeader, ErrorNote, Page, PageHeader, Field, Select } from '../components/ui';

export function PairDevice() {
  useAppliedTheme();
  const [token] = useState(() => window.location.hash.slice(1));
  const [receipt, setReceipt] = useState('');
  const [matchingCode, setMatchingCode] = useState('');
  const [available, setAvailable] = useState(false);
  const [format, setFormat] = useState('pkcs12');
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { window.history.replaceState(null, '', '/pair-device'); }, []);
  useEffect(() => { if (available) announce('Owner confirmed. The identity is ready to download once.'); }, [available]);
  useEffect(() => {
    if (!receipt || done) return;
    let closed = false;
    const poll = async () => {
      try {
        const result = await post<{ available: boolean }>('/api/device-pairing/status', { receipt });
        if (closed) return;
        setAvailable(result.available); setError(null);
      } catch (e) {
        if (closed) return;
        setAvailable(false); setError(e);
        if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429) return;
      }
      if (!closed) timer = setTimeout(poll, 3000);
    };
    let timer = setTimeout(poll, 3000);
    return () => { closed = true; clearTimeout(timer); };
  }, [receipt, done]);
  return <Page><PageHeader title="Pair this device" description="Use the private LAN address. Keep the trusted device nearby." />
    <Card><CardHeader title="Browser identity" subtitle="One-use import · expires within five minutes of owner confirmation" />
      <div className="space-y-4 p-4">
        {!token ? <p className="text-sm text-muted">Open a fresh pairing link from your trusted device. A refreshed page cannot recover a one-use capability.</p> : !receipt && !done ? <>
          <p className="text-sm text-muted">Request pairing, then ask the owner to confirm your device. Rynhult will provide an encrypted certificate to import in your operating system or browser.</p>
          <Button disabled={busy} onClick={async () => { setBusy(true); setError(null); try { const result = await post<{ receipt: string; matching_code: string }>('/api/device-pairing/claim', { token }); setReceipt(result.receipt); setMatchingCode(result.matching_code); announce('Waiting for owner confirmation.'); } catch (e) { setError(e); } finally { setBusy(false); } }}>Request pairing</Button>
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
