import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, isNotFound } from '../lib/api';
import { useBriefDraft, type BriefView, type Version } from '../lib/projectBrief';
import { Button, ErrorNote, Field, QueryState, Select } from './ui';

export function ProjectBriefChoice({ projectId, value, onChange, forGrant = true, onOpenBrief }: {
  projectId: number; value?: number; onChange: (version: number, mode?: 'prepare' | 'build') => void; forGrant?: boolean;
  onOpenBrief?: () => void;
}) {
  const query = useQuery({ queryKey: ['project-brief', projectId, undefined],
    queryFn: () => api<BriefView>(`/api/projects/${projectId}/brief`), retry: false });
  const draft = useBriefDraft(projectId);
  const explicit = useRef(value !== undefined);
  const head = useRef<number | undefined>(undefined);
  const [older, setOlder] = useState<Version[]>([]);
  const [more, setMore] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    if (!query.data) return;
    if (value === undefined || (!explicit.current && head.current !== query.data.current_version)) {
      onChange(query.data.current_version, query.data.current
        ? query.data.current.submitted.run_mode === 'build' ? 'build' : 'prepare' : undefined);
    } else if (head.current === undefined) {
      const version = [query.data.current, ...query.data.history].find(v => v?.version === value);
      onChange(value, version ? version.submitted.run_mode === 'build' ? 'build' : 'prepare' : undefined);
    }
    head.current = query.data.current_version;
  }, [query.data, value, onChange]);
  if (isNotFound(query.error)) return null;
  return <QueryState query={query}>{brief => {
    const versions = [...new Map([...(brief.current ? [brief.current] : []), ...brief.history, ...older]
      .filter(v => v.state === 'saved').map(v => [v.version, v])).values()].sort((a, b) => b.version - a.version);
    const selected = value ?? brief.current_version;
    const cursor = Math.min(...[...brief.history, ...older].map(v => v.version));
    return <div className="space-y-2">
      <Field label="Project brief version"><Select value={selected} onChange={event => {
        explicit.current = true;
        const number = Number(event.target.value);
        const version = versions.find(v => v.version === number);
        onChange(number, version ? version.submitted.run_mode === 'build' ? 'build' : 'prepare' : undefined);
      }}>
        <option value={0}>None — no pinned project brief</option>
        {versions.map(v => <option key={v.version} value={v.version}>Version {v.version}{v.version === brief.current_version ? ' (latest saved)' : ''}</option>)}
      </Select></Field>
      <p className="text-[13px] text-muted">{forGrant
        ? selected ? `This team will pin saved project brief version ${selected}.` : 'This team will start without a pinned project brief.'
        : selected ? `Selected saved project brief version ${selected}. Apply it to change this team's pinned scope.` : 'No project brief selected.'}</p>
      {draft && <p className="text-[13px] text-wait">{brief.current_version
        ? 'Unsaved brief changes in this tab are not included in the saved version.'
        : 'This project has only an unsaved draft in this tab. The team cannot read it.'}</p>}
      {!brief.current_version && !draft && <p className="text-[13px] text-muted">This project has no saved brief.</p>}
      <Link to={`/projects/${projectId}#project-brief`} onClick={onOpenBrief} className="inline-flex min-h-8 items-center text-[13px] text-accent underline pointer-coarse:min-h-11">
        {draft ? 'Save the project brief' : brief.current_version ? 'Review the project brief' : 'Add a brief on the project page'}
      </Link>
      {more && (brief.history.length === 20 || older.length > 0) && <Button disabled={busy} onClick={async () => {
        setBusy(true); setError(undefined);
        try {
          const next = await api<BriefView>(`/api/projects/${projectId}/brief?history_before=${cursor}`);
          setOlder(previous => [...previous, ...next.history]); setMore(next.history.length === 20);
        } catch (e) { setError(e); } finally { setBusy(false); }
      }}>Load older brief versions</Button>}
      <ErrorNote error={error} title="Couldn't load older brief versions." />
    </div>;
  }}</QueryState>;
}
