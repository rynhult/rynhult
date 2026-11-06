import { usePersonalTheme, resumeCustomAppearance } from './PersonalThemeRuntime';
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { previewHtml, type ThemeData, type ThemePreference } from '../lib/community-theme';
import { useRole } from '../lib/queries';
import { announce, Button, Card, Dialog, ErrorNote, Field, QueryState } from './ui';

const preferencePath = '/api/catalog/themes/preference';
/** Settings owner hook: <AppearanceThemes /> inside Account → Appearance. Same records as Catalog. */
export function AppearanceThemes({ context = 'catalog' }: { context?: 'catalog' | 'account' }) {
  const account = context === 'account';
  const { owner } = useRole(), client = useQueryClient();
  const { query: preference, person } = usePersonalTheme();
  const packages = useQuery({ queryKey: ['catalog-themes', person], enabled: !!person,
    queryFn: () => api<{ items: ThemeData[] }>('/api/catalog/themes', { cache: 'no-store' }) });
  const [file, setFile] = useState<File | null>(null), [review, setReview] = useState<ThemeData | null>(null);
  const [importing, setImporting] = useState(false), [mode, setMode] = useState<'light' | 'dark'>('light');
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [notice, setNotice] = useState('');
  const run = async (action: () => Promise<void>) => { setBusy(true); setError(null); try { await action(); } catch (error) { setError(error); } finally { setBusy(false); } };
  const close = () => { if (busy) return; const token = review?.token; setReview(null); if (token) void api('/api/catalog/themes/cancel', { json: { token } }).catch(() => {}); };
  const preview = async (item: ThemeData) => {
    setImporting(false);
    setReview(await api<ThemeData>('/api/catalog/themes/preview', { json: { id: item.manifest!.id, digest: item.manifest!.digest } }));
  };
  const change = async (token?: string) => {
    if (!preference.data) throw new Error('Reload your current preference before saving.');
    const value = await api<ThemePreference>(preferencePath, { method: 'PUT', json: { token, revision: preference.data.revision, request_id: crypto.randomUUID() } });
    if (token) resumeCustomAppearance();
    client.setQueryData(['personal-theme', person], value); setReview(null);
    setNotice(token ? 'Theme applied to your account.' : 'Account theme reset to built-in Rynhult.'); announce(token ? 'Theme applied.' : 'Theme reset.');
  };
  let document = '';
  try { if (review) document = previewHtml(review, mode); } catch { /* malformed data never enters an iframe or root */ }
  const reviewActions = review && <div className="theme-trusted flex flex-wrap gap-3">
          {importing ? <Button disabled={busy || !document} onClick={() => void run(async () => {
            await api('/api/catalog/themes/install', { json: { token: review.token } });
            await packages.refetch(); setReview(null); setNotice('Theme added. Preview it below before applying.');
          })}>Add theme to Catalog</Button> : <Button disabled={busy || !document || !preference.data} onClick={() => void run(() => change(review.token))}>Apply to my account</Button>}
          <Button disabled={busy} onClick={close}>Cancel</Button>
        </div>;
  return <section aria-label="Appearance and themes" className="theme-trusted space-y-4 rounded-lg border border-line bg-panel p-4 text-fg">
    <h2 className="text-base font-semibold">{account ? 'Installed themes' : 'Appearance and themes'}</h2>
    <p>Personal presentation only. System, Light and Dark still follow your appearance control. Terminal palette stays built in.</p>
    <p className="text-xs text-muted">Workspace overrides and executable plugins are unavailable in this phase.</p>
    <QueryState query={preference}>{value => <div className="flex flex-wrap items-center gap-3">
      <p className="min-w-0 max-w-full [overflow-wrap:anywhere]">Active theme: {value.effective?.manifest?.name ?? 'Built-in Rynhult'}{value.unavailable ? ' · selected package unavailable' : ''}</p>
      <Button disabled={busy} onClick={() => void run(() => change())}>Reset account theme</Button>
    </div>}</QueryState>
    <p role="status">{notice}</p><ErrorNote error={error} />
    {owner && !account && <Card><form className="space-y-3 p-4" onSubmit={event => { event.preventDefault(); if (!file || busy) return;
      void run(async () => { if (file.size > 2 * 1024 * 1024) throw new Error('Theme archive limit is 2 MiB.');
        const result = await api<ThemeData>('/api/catalog/themes/review', { method: 'POST', body: file, headers: { 'Content-Type': 'application/zip' } });
        setImporting(true); setReview(result); }); }}>
      <Field label="Theme package (.zip)" hint="Local declarative package, at most 2 MiB. Import never applies a theme.">
        <input type="file" name="theme-package" accept=".zip,application/zip" disabled={busy} onChange={event => setFile(event.target.files?.[0] ?? null)} />
      </Field>
      <Button type="submit" disabled={busy || !file}>Review theme package</Button>
    </form></Card>}
    <QueryState query={packages}>{catalog => <div className={account ? "grid gap-3 md:grid-cols-2" : "space-y-3"}>{!catalog.items.length && <p>No community themes installed. Built-in Rynhult is available.</p>}
      {catalog.items.map(item => <article key={item.manifest!.digest} aria-label={`Theme ${item.manifest!.id} ${item.manifest!.version}`} className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-3">
        <div className="min-w-0 flex-1 [overflow-wrap:anywhere]"><p>{item.manifest!.name} · {item.manifest!.version}</p><p className="text-xs text-muted">{item.manifest!.id} · {item.unavailable ? 'Unavailable; using built-in fallback' : 'Installed · Publisher not verified'}</p></div>
        <Button aria-label={`Preview ${item.manifest!.name} ${item.manifest!.version}`} disabled={busy || item.unavailable} onClick={() => void run(() => preview(item))}>Preview</Button>
      </article>)}
    </div>}</QueryState>
    <Dialog open={!!review} onClose={close} dismissible={!busy} title={importing ? 'Review theme import' : 'Preview account theme'} wide footer={reviewActions} description="Preview uses synthetic examples, never your sessions or files.">
      {review && <div ref={node => { node?.closest('[role="dialog"]')?.classList.add('theme-trusted'); }} className="theme-trusted space-y-3 bg-panel p-3 text-fg">
        <p className="[overflow-wrap:anywhere]">{review.manifest?.name} · {review.manifest?.version}</p><p>Publisher not verified</p>
        <p className="break-words text-xs">{review.manifest?.author} · {review.manifest?.source?.description} · Licence {review.manifest?.license?.spdx}</p>
        <p className="break-all text-xs">{review.manifest?.digest}</p>
        <p>Both appearances validated. Approved semantic tokens only; no scripts, unrestricted CSS or remote assets.</p>
        <Field label="Preview appearance"><select className="mt-1.5 block min-h-11 rounded-md border border-line-input bg-bg px-3 text-fg" value={mode} onChange={event => setMode(event.target.value as 'light' | 'dark')}><option value="light">Light</option><option value="dark">Dark</option></select></Field>
        {document ? <iframe title="Theme preview" sandbox="" referrerPolicy="no-referrer" srcDoc={document} className="h-96 w-full rounded-md border border-line-input" /> : <p role="alert">Preview unavailable. Nothing has been applied.</p>}
        <ErrorNote error={error} />

      </div>}
    </Dialog>
  </section>;
}
