import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, isNotFound } from '../lib/api';
import { labels, type Version } from '../lib/projectBrief';
import { Button, Card, CardHeader, ErrorNote, QueryState } from './ui';
import { ProjectBriefChoice } from './ProjectBriefChoice';

export function TeamBrief({ teamId, projectId }: { teamId: string; projectId: number }) {
  const client = useQueryClient();
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [selected, setSelected] = useState<number>();
  const query = useQuery({ queryKey: ['team-brief', teamId], queryFn: () => api<(Version & { authority?: string; run_mode: string | null; suggested_run_mode: 'prepare' | 'build'; available_version: number; project_id: number; interpretations: { text: string; references: Record<string, { status: string; note: string }> }[] }) | null>(`/api/teams/${teamId}/brief`), retry: false });
  useEffect(() => { setSelected(query.data?.version); }, [query.data?.version]);
  if (isNotFound(query.error)) return null;
  return <QueryState query={query}>{brief => <Card className="min-w-0 space-y-3 p-4">
    <CardHeader title={brief ? `Pinned project brief · version ${brief.version}` : 'No pinned project brief'}
      subtitle={brief ? `The brief suggested ${brief.suggested_run_mode === 'prepare' ? 'Prepare' : 'Build'}.` : 'This team has no saved project brief pinned to its scope.'} />
    <p className="text-xs text-muted">Proposed versions are not selectable as saved scope. Use the proposal review above or Version history on the project to merge their fields onto the current brief, even when their base is older.</p>
    <ProjectBriefChoice key={brief?.version ?? 0} projectId={projectId} value={selected} onChange={setSelected} forGrant={false} />
    <Button type="button" disabled={pending || selected === undefined || selected === (brief?.version ?? 0)} onClick={async () => {
      setPending(true); setError(undefined);
      try {
        await api(`/api/teams/${teamId}/brief/pin`, { json: { base_version: brief?.version ?? 0, brief_version: selected } });
        await client.invalidateQueries({ queryKey: ['team-brief', teamId] });
      } catch (e) { setError(e); } finally { setPending(false); }
    }}>Apply selected brief version</Button>
    <ErrorNote error={error} title="Couldn't apply the brief version." />
    <p className="text-xs text-muted">The effective run mode is controlled in Settings, not by this suggestion.</p>
    {brief && <>
    {brief.authority && <p role="note" className="text-[13px] text-wait">{brief.authority}</p>}
    <details open><summary className="min-h-8 cursor-pointer py-1">Person's submitted text</summary><pre className="whitespace-pre-wrap break-words">{Object.entries(brief.submitted).map(([key, value]) => `${labels[key as keyof typeof labels] ?? key}: ${value}`).join('\n\n')}</pre></details>
    <section aria-label="Brief references" className="space-y-2"><h3 className="font-medium">References</h3>
      {!brief.references.length && <p className="text-[13px] text-muted">No references attached.</p>}
      {brief.references.map(ref => <div key={ref.reference_id} className="break-words text-[13px]">
        <p>{ref.reference_id} · {ref.caption}</p>{ref.url && <p className="text-muted">{ref.url}</p>}
        <p className="whitespace-pre-wrap">{ref.guidance}</p>
      </div>)}
    </section>
    <h3 className="font-medium">Lead interpretation (not accepted scope)</h3>
    {!brief.interpretations.length && <p className="text-[13px] text-muted">The lead has not interpreted this brief yet.</p>}
    {brief.interpretations.map((i, n) => <div key={n} className="space-y-2"><p className="whitespace-pre-wrap break-words text-[13px]">{i.text}</p>
      {Object.entries(i.references).map(([ref, observation]) => <p key={ref} className="text-[13px] text-muted">{ref}: {observation.status} · {observation.note}</p>)}</div>)}
    <p className="text-xs text-muted">Accepted requirements and constraints stay in Plan. Clarifying questions use the Working list tab.</p>
    </>}
  </Card>}</QueryState>;
}
