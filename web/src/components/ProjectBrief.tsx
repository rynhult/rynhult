/** Project-owned intake; DB versions are authoritative, references stay private. */
import { useEffect, useId, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { BriefProposalReview } from './BriefProposalReview';
import { api, isNotFound } from '../lib/api';
import { Button, Card, CardHeader, Dialog, ErrorNote, Field, Input, Note, QueryState, Select, Textarea } from './ui';

import { keepBriefDraft, labels, readBriefDraft, type BriefView, type Fields, type Reference, type SavedBriefEvent, type Version } from '../lib/projectBrief';
import { BriefDocuments } from './BriefDocuments';
import { useBriefNavigationConsent, useUnsavedBrief } from './UnsavedBriefGuard';
interface Saved extends Version { export: { ok: boolean; note?: string; path: string } }

export function ProjectBrief({ projectId, compact = false, onVersion }: { projectId: number; compact?: boolean; onVersion?: (version: number, mode?: 'prepare' | 'build') => void }) {
  const client = useQueryClient();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [localReview, setLocalReview] = useState(0);
  const allowBriefNavigation = useBriefNavigationConsent();
  const reviewNumber = compact ? localReview : Number(search.get('brief_review')) || 0;
  const [warning, setWarning] = useState(false);
  const [saveAfterWarning, setSaveAfterWarning] = useState(false);
  const warned = useRef(new Set<number>());
  const reviewQuery = useQuery({ queryKey: ['brief-proposal-review', projectId, reviewNumber], enabled: reviewNumber > 0,
    queryFn: () => api<BriefView>(`/api/projects/${projectId}/brief?review_version=${reviewNumber}`), retry: false });
  function reviewNavigation(number: number) {
    if (compact) { setLocalReview(number); return; }
    const next = new URLSearchParams(search);
    if (number) next.set('brief_review', String(number)); else next.delete('brief_review');
    allowBriefNavigation(location.pathname);
    navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash || '#project-brief' }, { replace: true });
  }
  function openReview(number: number) { setWarning(false); reviewNavigation(number); }
  function closeReview() { reviewNavigation(0); }
  const [historyBefore, setHistoryBefore] = useState<number>();
  const query = useQuery({ queryKey: ['project-brief', projectId, historyBefore], queryFn: () => api<BriefView>(`/api/projects/${projectId}/brief${historyBefore ? `?history_before=${historyBefore}` : ''}`), retry: false, refetchInterval: 5000 });
  // An unsaved brief is kept for this tab (break-it 14): a refresh or Back brings it back, like working-list drafts.
  const [restored] = useState(() => readBriefDraft(projectId));
  const [draft, setDraft] = useState<Fields>(restored?.draft ?? { product: '', run_mode: 'prepare' });
  const [references, setReferences] = useState<Reference[]>(restored?.references ?? []);
  const [base, setBase] = useState(restored?.base ?? 0);
  const [dirty, setDirty] = useState(!!restored);
  useUnsavedBrief(dirty);
  const formRef = useRef<HTMLDivElement>(null);
  const saveHint = useId();
  const [restoredNote, setRestoredNote] = useState(!!restored);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [caption, setCaption] = useState('');
  const [guidance, setGuidance] = useState('');
  const [url, setUrl] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [fileSelection, setFileSelection] = useState(0);
  const feedback = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (error) {
      feedback.current?.focus({ preventScroll: true });
      feedback.current?.scrollIntoView({ block: 'center' });
    }
  }, [error]);
  const [exportPath, setExportPath] = useState(restored?.exportPath ?? 'docs/PROJECT-BRIEF.md');
  const liveDraft = useRef({ draft, references, base, exportPath });
  liveDraft.current = { draft, references, base, exportPath };
  useEffect(() => { keepBriefDraft(projectId, dirty ? { draft, references, base, exportPath } : null); }, [projectId, dirty, draft, references, base, exportPath]);
  useEffect(() => {
    const acknowledge = (event: Event) => {
      const change = (event as CustomEvent<SavedBriefEvent>).detail;
      if (change.projectId !== projectId || change.draft.base !== base
        || JSON.stringify(change.draft.draft) !== JSON.stringify(draft)
        || JSON.stringify(change.draft.references) !== JSON.stringify(references)
        || (change.draft.exportPath && change.draft.exportPath !== exportPath)) return;
      setDirty(false); setRestoredNote(false); setBase(change.saved.version);
    };
    window.addEventListener('acc-brief-saved', acknowledge);
    return () => window.removeEventListener('acc-brief-saved', acknowledge);
  }, [projectId, draft, references, base, exportPath]);
  useEffect(() => {
    if (!query.data || dirty || query.data.current_version < base) return;
    setDraft(query.data.current?.submitted ?? { product: '', run_mode: 'prepare' });
    setReferences(query.data.current?.references ?? []);
    setBase(query.data.current_version);
    setExportPath(query.data.export_path);
  }, [query.data, dirty, base]);
  useEffect(() => { if (query.data) onVersion?.(query.data.current_version, query.data.current ? (query.data.current.submitted.run_mode === 'build' ? 'build' : 'prepare') : undefined); }, [query.data?.current_version]);
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(undefined); setStatus('');
    try { await action(); } catch (e) { setError(e); await query.refetch(); } finally { setBusy(false); }
  }
  async function refresh() { await client.invalidateQueries({ queryKey: ['project-brief', projectId] }); }
  async function saved(value: Saved) {
    setDirty(false); setRestoredNote(false); setBase(value.version); setDraft(value.submitted); setReferences(value.references);
    setStatus(value.export.ok ? `Brief version ${value.version} saved. Markdown exported to ${value.export.path}.` : `Brief version ${value.version} saved. ${value.export.note ?? 'Repository export needs attention.'}`);
    await refresh();
  }
  if (isNotFound(query.error)) return null;
  const pendingProposal = query.data?.pending_proposal;
  function warnPending(saving = false) {
    if (!pendingProposal || warned.current.has(pendingProposal.version)) return false;
    warned.current.add(pendingProposal.version); setSaveAfterWarning(saving); setWarning(true); return true;
  }
  function saveForm() {
    void run(async () => {
      const latest = await query.refetch();
      if (latest.error) throw latest.error;
      const proposal = latest.data?.pending_proposal;
      if (proposal && !warned.current.has(proposal.version)) {
        warned.current.add(proposal.version); setSaveAfterWarning(true); setWarning(true); return;
      }
      const snapshot = liveDraft.current;
      const value = await api<Saved>(`/api/projects/${projectId}/brief`, { json: { version: snapshot.base, fields: snapshot.draft,
        references: snapshot.references.map(r => r.reference_id), export_path: snapshot.exportPath, expected_next_version: latest.data?.next_version } });
      if (liveDraft.current.draft === snapshot.draft && liveDraft.current.references === snapshot.references && liveDraft.current.exportPath === snapshot.exportPath) await saved(value);
      else { setBase(value.version); setStatus(`Brief version ${value.version} saved. Your newer edits are kept as an unsaved draft.`); await refresh(); }
    });
  }
  const missingProduct = !draft.product?.trim();
  const form = <div ref={formRef} className="min-w-0 space-y-4">
    {pendingProposal && <section aria-label="Proposed project brief" className="space-y-2 rounded-lg border border-wait/40 bg-wait/5 p-3">
      <p className="font-medium">{pendingProposal.source === 'documents' ? 'A brief drafted from your documents is ready to review' : 'An imported brief is ready to review'}</p>
      <Button onClick={() => openReview(pendingProposal.version)}>Review</Button>
    </section>}
    {reviewNumber > 0 && reviewQuery.isPending && <p role="status">Loading proposed brief…</p>}
    {reviewNumber > 0 && <ErrorNote error={reviewQuery.error} title="Couldn't load this proposed brief." />}
    {reviewQuery.data?.review_proposal && reviewNumber > 0 && <BriefProposalReview key={`${projectId}:${reviewNumber}`} version={reviewQuery.data.review_proposal}
      current={query.data?.current ?? null} currentVersion={query.data?.current_version ?? 0} busy={busy} error={error} dirty={dirty} onClose={closeReview}
      onAccept={(fields, edits, version) => void run(async () => {
        const value = await api<Saved>(`/api/projects/${projectId}/brief/versions/${reviewNumber}/accept`, { json: { version, fields, edits } });
        if (dirty) { setStatus(`Brief version ${value.version} accepted. Your unsaved form draft is kept; review the current saved version before saving it.`); await refresh(); }
        else await saved(value);
        await client.invalidateQueries({ queryKey: ['team-brief'] }); closeReview();
      })} />}
    <Dialog open={warning} onClose={() => setWarning(false)} title="There is a proposed brief from your documents; review it first?" dismissible={false}
      footer={<><Button onClick={() => { setWarning(false); if (saveAfterWarning) saveForm(); }}>Continue</Button><Button variant="primary" onClick={() => { if (pendingProposal) openReview(pendingProposal.version); else setWarning(false); }}>Review</Button></>}>
      <p>Your typed text is kept. Review the cited proposal before replacing it with a new brief.</p>
    </Dialog>
    <p className="text-[13px] text-muted">Start from an idea or describe one feature. Only Product or feature is required. “I don't know, recommend something” is a valid answer. Incomplete drafts save.</p>
    {(!!error || status) && <div ref={feedback} role="region" aria-label="Project brief feedback" tabIndex={-1} className="space-y-2 rounded-md focus-visible:outline-2 focus-visible:outline-accent">
      <ErrorNote error={error} title="Couldn't update the project brief." />
      {status && <p role="status" className="text-[13px] text-muted">{status}</p>}
    </div>}
    <p className={dirty || !base ? 'text-[13px] font-medium text-wait' : 'text-[13px] font-medium'}>{dirty || !base ? 'Draft, not saved' : `Saved as version ${base}`}</p>
    <p className="text-xs text-muted">Editing from version {base}. Saving keeps every earlier version.</p>
    {dirty && <div className="text-xs text-muted">
      {restoredNote && <p role="status">Unsaved draft restored.</p>}
      <Button size="sm" variant="ghost" onClick={() => { setRestoredNote(false); setDirty(false); }}>Discard draft</Button>
    </div>}
    {query.data && query.data.current_version !== base && <div role="alert" className="space-y-2 text-[13px] text-wait">
      <p>The saved brief is now version {query.data.current_version}. Your unsaved text is kept.</p>
      <details><summary className="min-h-11 cursor-pointer py-2.5">Review the current submitted text before resolving the conflict</summary><pre className="whitespace-pre-wrap break-words">{JSON.stringify(query.data.current?.submitted, null, 2)}</pre></details>
      <Button type="button" onClick={() => setBase(query.data!.current_version)}>Use the current version as the base for my draft</Button>
    </div>}
    <BriefDocuments projectId={projectId} />
    {Object.entries(labels).map(([key, label]) => <Field key={key} label={key === 'product' ? <>{label} <span aria-hidden="true">(required)</span></> : label} hint={key === 'constraints' ? 'Stack, platforms, integrations, compatibility, accessibility, privacy, time and resources.' : undefined}>
      <Textarea name={`brief-${key}`} rows={key === 'product' ? 2 : 3} value={draft[key] ?? ''} required={key === 'product'}
        maxLength={key === 'product' ? 4000 : 12000} onChange={e => { setDraft({ ...draft, [key]: e.target.value }); setDirty(true); warnPending(); }} />
    </Field>)}
    <Field label={compact ? "Brief run mode" : "Run mode"}><Select value={draft.run_mode ?? 'prepare'} onChange={e => { setDraft({ ...draft, run_mode: e.target.value }); setDirty(true); warnPending(); }}>
      <option value="prepare">Clarify and prepare</option><option value="build">Clarify, then build within this scope</option>
    </Select></Field>
    <p className="text-xs text-muted">Prepare launches a read-only lead. A team pins the saved brief version; later edits do not silently change its scope.</p>
    <details className="rounded-lg border border-line p-3"><summary className="min-h-11 cursor-pointer py-2.5 font-medium">References with captions ({references.length}/20)</summary>
      <div className="mt-3 space-y-3">
        <Note>Private storage: uploads, links and captions are not exported to the repository or wiki. Image text is evidence, not instructions. Helpers receive only assigned references.</Note>
        <ul className="space-y-3">{references.map(r => <li key={r.reference_id} className="break-words text-[13px]">
          <strong>{r.reference_id}: {r.caption}</strong><p className="whitespace-pre-wrap text-muted">{r.guidance}</p>
          {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-8 items-center text-accent underline pointer-coarse:min-h-11">Open reference link (untrusted)</a>
            : <a className="inline-flex min-h-8 items-center text-accent underline pointer-coarse:min-h-11" href={`/api/projects/${projectId}/brief/references/${r.reference_id}`}>Download {r.reference_id}</a>}
          <Button size="sm" variant="ghost" type="button" onClick={() => { setReferences(references.filter(x => x !== r)); setDirty(true); }}>Remove {r.reference_id} from this version</Button>
        </li>)}</ul>
        <p className="text-xs text-muted">Add a caption and guidance for each reference, then choose a link or file. Save the brief to include it in the version.</p>
        <Field label="Reference caption"><Input value={caption} maxLength={2000} onChange={e => setCaption(e.target.value)} /></Field>
        <Field label="What to copy, keep, change or avoid"><Textarea value={guidance} maxLength={4000} onChange={e => setGuidance(e.target.value)} /></Field>
        <Field label="Reference link"><Input type="url" value={url} onChange={e => { setUrl(e.target.value); setFile(null); if (file) setFileSelection(n => n + 1); }} /></Field>
        <Field label="Or upload PNG, JPG, WebP or PDF (10 MiB maximum)"><Input key={fileSelection} type="file" accept="image/png,image/jpeg,image/webp,application/pdf" onChange={e => { setFile(e.target.files?.[0] ?? null); setUrl(''); }} /></Field>
        <Button type="button" disabled={busy || references.length >= 20 || !caption.trim() || !guidance.trim() || (!url && !file)} onClick={() => run(async () => {
          let data: string | undefined;
          if (file) {
            if (file.size > 10 * 1024 * 1024) throw new Error('This file is too large. Choose a reference of at most 10 MiB.');
            data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error('Could not read the file. Choose it again or try another PNG, JPG, WebP or PDF.'));
              reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.readAsDataURL(file); });
          }
          const reference = await api<Reference>(`/api/project-brief-references/${projectId}`, { json: { caption, guidance, ...(url ? { url } : { data, media_type: file?.type }) } });
          setReferences([...references, reference]); setCaption(''); setGuidance(''); setUrl(''); setFile(null); setFileSelection(n => n + 1); setDirty(true);
          setStatus(`${reference.reference_id} stored privately. Save the brief to include it in this version.`);
        })}>Add reference</Button>
      </div>
    </details>
    <details className="rounded-lg border border-line p-3"><summary className="min-h-11 cursor-pointer py-2.5 font-medium">Markdown import and export</summary>
      <div className="mt-3 space-y-3">
        <Field label="Markdown export path"><Input value={exportPath} onChange={e => setExportPath(e.target.value)} /></Field>
        <p className="text-xs text-muted">Saving exports the submitted form text. Existing independently edited Markdown is never overwritten silently. References remain private.</p>
        <Field label="Import Markdown as a proposed version"><Textarea value={source} maxLength={256000} rows={5} onChange={e => setSource(e.target.value)} /></Field>
        <Button type="button" disabled={busy || !source.trim()} onClick={() => run(async () => {
          const value = await api<Version>(`/api/projects/${projectId}/brief/import`, { json: { markdown: source, base_version: base, references: references.map(r => r.reference_id) } });
          setStatus(`Proposed version ${value.version}; ${value.conflicts.length ? `conflicts: ${value.conflicts.join(', ')}` : 'no field conflicts'}. Use Review to compare fields before accepting.`);
          await refresh();
        })}>Propose imported Markdown</Button>
      </div>
    </details>
    {missingProduct && <p id={saveHint} className="text-[13px] text-wait">Add what you want to build in Product or feature to save</p>}
    <Button type="button" variant="primary" className="aria-disabled:opacity-50" disabled={busy} aria-disabled={missingProduct}
      aria-describedby={missingProduct ? saveHint : undefined} onClick={() => {
      if (missingProduct) { formRef.current?.querySelector<HTMLTextAreaElement>('[name="brief-product"]')?.focus(); return; }
      if (!warnPending(true)) saveForm(); }}>{busy ? 'Saving…' : 'Save project brief'}</Button>
    <details className="rounded-lg border border-line p-3"><summary className="min-h-11 cursor-pointer py-2.5 font-medium">Version history</summary>
      <ul className="mt-3 space-y-4">{query.data?.history.map(v => <li key={v.version} className="text-[13px]">
        <strong>Version {v.version} · {v.state} · {v.actor}{v.source && v.source !== 'person' ? ` · ${v.source}` : ''}</strong>
        {v.citations && <ul className="text-xs text-muted">{Object.entries(v.citations).map(([field, sources]) => <li key={field}>{labels[field as keyof typeof labels] ?? field}: {sources.map(source => `${v.references.find(r => r.reference_id === source.reference)?.caption ?? source.reference} (${source.reference}, ${source.location})`).join('; ')}</li>)}</ul>}
        {v.conflicts.length > 0 && <p className="text-wait">Conflicts with current brief: {v.conflicts.join(', ')}</p>}
        <details><summary className="min-h-11 cursor-pointer py-2.5">Read submitted text for version {v.version}</summary><pre tabIndex={0} role="region" aria-label={`Submitted text for brief version ${v.version}`} className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md focus-visible:outline-2 focus-visible:outline-accent">{Object.entries(v.submitted).map(([k, text]) => `${labels[k as keyof typeof labels] ?? k}: ${text}`).join('\n\n')}</pre></details>
        {v.state === 'proposed' && <Button onClick={() => openReview(v.version)}>Review proposed version {v.version}</Button>}
        {v.state === 'proposed' && <AcceptFields version={v} current={query.data!.current} busy={busy}
          onAccept={fields => run(async () => {
            await saved(await api<Saved>(`/api/projects/${projectId}/brief/versions/${v.version}/accept`, { json: { version: query.data!.current_version, fields } }));
          })} />}
      </li>)}</ul>
      <div className="mt-3 flex flex-wrap gap-2">
        {query.data?.history.length === 20 && <Button type="button" onClick={() => setHistoryBefore(query.data!.history.at(-1)!.version)}>Older versions</Button>}
        {historyBefore && <Button type="button" onClick={() => setHistoryBefore(undefined)}>Latest versions</Button>}
      </div>
    </details>
  </div>;
  return <QueryState query={query}>{() => compact ? <details className="rounded-lg border border-line p-3"><summary className="min-h-11 cursor-pointer py-2.5 font-medium">Add a project brief</summary><div className="mt-3">{form}</div></details>
    : <Card className="min-w-0 p-4"><CardHeader title="Project brief" subtitle="Your submitted scope, kept separate from the lead's interpretation" />{form}</Card>}</QueryState>;
}
/** Imported Markdown is repository text: the person takes it field by field, never as a whole version. */
function AcceptFields({ version, current, busy, onAccept }: { version: Version; current: Version | null; busy: boolean; onAccept: (fields: string[]) => void }) {
  const names = [...Object.keys(labels), 'run_mode'].filter(key => Object.hasOwn(version.submitted, key)
    && version.submitted[key] !== current?.submitted[key]);
  if (version.references.length && version.references.map(r => r.reference_id).join() !== (current?.references ?? []).map(r => r.reference_id).join()) names.push('references');
  const [chosen, setChosen] = useState<string[]>([]);
  if (!names.length) return <p className="text-muted">This proposal changes no field.</p>;
  return <fieldset className="mt-2 space-y-1">
    <legend className="text-muted">{version.source === 'documents' ? 'Document proposal, not yours until you accept it. Choose the fields to take:' : 'Repository text, not yours until you accept it. Choose the fields to take:'}</legend>
    {names.map(key => <label key={key} className="flex min-h-8 items-center gap-2">
      <input type="checkbox" checked={chosen.includes(key)} onChange={e => setChosen(e.target.checked ? [...chosen, key] : chosen.filter(x => x !== key))} />
      {key === 'references' ? 'References' : key === 'run_mode' ? 'Run mode' : labels[key as keyof typeof labels]}
    </label>)}
    <Button className="h-auto! min-h-8 max-w-full whitespace-normal! text-left" type="button" disabled={busy || !chosen.length}
      onClick={() => onAccept(chosen)}>Accept {chosen.length || 'the chosen'} field{chosen.length === 1 ? '' : 's'} from version {version.version}</Button>
  </fieldset>;
}
