import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, isNotFound } from '../lib/api';
import { labels, type BriefView, type Fields, type Version } from '../lib/projectBrief';
import { Button, Dialog, ErrorNote, Field, Select, Textarea } from './ui';

export const proposalUrl = (id: number, version: number) => `/projects/${id}?brief_review=${version}#project-brief`;
export function BriefProposalBanner({ projectId }: { projectId: number }) {
  const query = useQuery({ queryKey: ['project-brief', projectId, undefined], queryFn: () => api<BriefView>(`/api/projects/${projectId}/brief`), refetchInterval: 5000, retry: false });
  const proposal = query.data?.pending_proposal ?? query.data?.latest_proposal;
  if (isNotFound(query.error)) return null;
  return <>
    <ErrorNote error={query.error} title="Couldn't check for a proposed brief." />
    {proposal && <section aria-label="Proposed project brief" className="space-y-2 rounded-lg border border-wait/40 bg-wait/5 p-3">
      <p className="font-medium">{query.data?.pending_proposal
        ? proposal.source === 'documents' ? 'A brief drafted from your documents is ready to review' : 'An imported brief is ready to review'
        : `Version ${proposal.version} is a proposal, not a saved brief. Review its fields before selecting a saved version.`}</p>
      <Link className="inline-flex min-h-11 items-center text-accent underline" to={proposalUrl(projectId, proposal.version)}>Review proposed brief version {proposal.version}</Link>
    </section>}
  </>;
}

export function BriefProposalReview({ version, current, currentVersion, busy, error, dirty, onClose, onAccept }: {
  version: Version; current: Version | null; currentVersion: number; busy: boolean; error: unknown; dirty: boolean;
  onClose: () => void; onAccept: (fields: string[], edits: Fields, base: number) => void;
}) {
  const [baseline, setBaseline] = useState({ current, number: currentVersion });
  const [choices, setChoices] = useState<Record<string, 'accept' | 'edit' | 'skip'>>({});
  const [edits, setEdits] = useState<Fields>({});
  // An omitted proposal field is not a proposed deletion. Accept all preserves all unmentioned fields.
  const names = [...Object.keys(labels), 'run_mode'].filter(key => Object.hasOwn(version.submitted, key));
  if (version.references.length) names.push('references');
  const displayed = [...Object.keys(labels), 'run_mode', 'references'].filter(key => names.includes(key) || (key === 'references' ? !!baseline.current?.references.length : Object.hasOwn(baseline.current?.submitted ?? {}, key)));
  const chosen = names.filter(key => choices[key] === 'accept' || choices[key] === 'edit');
  const changed = baseline.number !== currentVersion;
  const title = (key: string) => key === 'references' ? 'References' : key === 'run_mode' ? 'Run mode' : labels[key as keyof typeof labels];
  const text = (row: Version | null, key: string) => key === 'references'
    ? row?.references.map(r => `${r.reference_id}: ${r.caption}`).join('\n') || 'None'
    : row?.submitted[key] ?? 'Not supplied';
  const submit = (all = false) => {
    const fields = all ? names : chosen;
    const selectedEdits = Object.fromEntries(fields.filter(key => choices[key] === 'edit').map(key => [key, edits[key] ?? version.submitted[key]]));
    onAccept(fields, selectedEdits, baseline.number);
  };
  return <Dialog open onClose={() => { if (!busy) onClose(); }} title={`Review proposed brief · version ${version.version}`} wide="xl" dismissible={false}
    footer={<><Button disabled={busy} onClick={onClose}>Close review</Button>
      <Button disabled={busy || changed} onClick={() => submit(true)}>Accept all</Button>
      <Button variant="primary" disabled={busy || changed || !chosen.length} onClick={() => submit()}>{busy ? 'Saving…' : 'Save reviewed fields'}</Button></>}>
    <div className="min-w-0 space-y-4">
      <p className="text-sm text-muted">Proposal base version {version.base_version} · current version {baseline.number}. Save reviewed fields merges your choices onto the current brief, keeping skipped and unmentioned fields. Accept all takes every supplied field, including skipped ones. References are added, not removed.</p>
      {dirty && <p className="text-sm text-wait">Your unsaved form draft is kept separately. It is not included in this acceptance.</p>}
      {changed && <div role="alert" className="space-y-2"><p>The current brief changed to version {currentVersion}. Review the new values before accepting.</p><Button onClick={() => setBaseline({ current, number: currentVersion })}>Rebase onto current</Button></div>}
      <ErrorNote error={error} title="Couldn't accept proposed fields." />
      {displayed.map(key => <fieldset key={key} className="min-w-0 space-y-2 rounded-lg border border-line p-3">
        <legend className="px-1 font-medium">{title(key)}</legend>
        <div className="grid min-w-0 gap-3 md:grid-cols-2">
          <div className="min-w-0"><h3 className="text-xs font-medium text-muted">Current</h3><p className="whitespace-pre-wrap break-words text-sm">{text(baseline.current, key)}</p></div>
          <div className="min-w-0"><h3 className="text-xs font-medium text-muted">Proposal</h3><p className="whitespace-pre-wrap break-words text-sm">{text(version, key)}</p>
            <ul className="mt-2 space-y-1 text-xs text-muted">{version.citations?.[key]?.map((cite, i) => <li key={i}>{version.references.find(r => r.reference_id === cite.reference)?.caption ?? cite.reference} ({cite.reference}, {cite.location})</li>)}</ul>
          </div>
        </div>
        {!names.includes(key) && <p className="text-xs text-muted">No proposed change; the current value is kept.</p>}
        {names.includes(key) && <div role="group" aria-label={`${title(key)} decision`} className="flex flex-wrap gap-2">
          <Button size="sm" variant={choices[key] === 'accept' ? 'primary' : 'secondary'} disabled={busy} aria-pressed={choices[key] === 'accept'} onClick={() => setChoices({ ...choices, [key]: 'accept' })}>Accept {title(key)}</Button>
          {key !== 'references' && <Button size="sm" variant={choices[key] === 'edit' ? 'primary' : 'secondary'} disabled={busy} aria-pressed={choices[key] === 'edit'} onClick={() => { setChoices({ ...choices, [key]: 'edit' }); setEdits({ ...edits, [key]: edits[key] ?? version.submitted[key] }); }}>Edit {title(key)}</Button>}
          <Button size="sm" variant={!choices[key] || choices[key] === 'skip' ? 'primary' : 'secondary'} disabled={busy} aria-pressed={!choices[key] || choices[key] === 'skip'} onClick={() => setChoices({ ...choices, [key]: 'skip' })}>Skip {title(key)}</Button>
        </div>}
        {choices[key] === 'edit' && <Field label={`Edit ${title(key)}`}>
          {key === 'run_mode' ? <Select disabled={busy} value={edits[key]} onChange={e => setEdits({ ...edits, [key]: e.target.value })}><option value="prepare">Prepare</option><option value="build">Build</option></Select>
            : <Textarea disabled={busy} rows={3} value={edits[key]} maxLength={key === 'product' ? 4000 : 12000} onChange={e => setEdits({ ...edits, [key]: e.target.value })} />}
        </Field>}
      </fieldset>)}
    </div>
  </Dialog>;
}
