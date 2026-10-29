import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../lib/api';
import { Dialog, Field, QueryState, Select } from './ui';

/** Text only: published content never fetches images or runs repository HTML. */
export function GroupPublished({ groupId, project, close }: {
  groupId: string; project: { id: number; name: string }; close: () => void;
}) {
  const base = `/api/groups/${groupId}/projects/${project.id}`;
  const [commit, setCommit] = useState('');
  const [selectedRef, setSelectedRef] = useState('');
  const [path, setPath] = useState('');
  const [view, setView] = useState('files');
  const [compare, setCompare] = useState('');
  const refs = useQuery({ queryKey: ['published-refs', groupId, project.id], queryFn: () => api<Record<string, string>>(base + '/exchange/refs') });
  const tree = useQuery({ queryKey: ['published-tree', groupId, project.id, commit], enabled: !!commit,
    queryFn: () => api<string[]>(base + `/published/tree?commit=${commit}`) });
  const file = useQuery({ queryKey: ['published-text', groupId, project.id, commit, path], enabled: !!commit && !!path,
    queryFn: () => api<{ content: string }>(base + `/published/file?commit=${commit}&path=${encodeURIComponent(path)}`) });
  const diff = useQuery({ queryKey: ['published-diff', groupId, project.id, compare, commit], enabled: !!compare && !!commit, queryFn: () => api<{ diff: string }>(base + `/published/diff?base=${compare}&commit=${commit}`) });
  return <Dialog open onClose={close} title={`${project.name} · Published`} description="Read committed text at a published commit. Review source before running it. Launch reviewed source from the group work board.">
    <div className="space-y-4">
      <QueryState query={refs} isEmpty={items => !Object.keys(items).length} empty={<p className="text-sm text-muted">No published commits yet. A member must explicitly publish committed work.</p>}>
        {items => <Field label="Published branch or tag"><Select value={selectedRef} onChange={e => { setSelectedRef(e.target.value); setCommit(items[e.target.value] ?? ''); setPath(''); }}>
          <option value="">Choose published commit…</option>{Object.entries(items).map(([ref, sha]) => <option key={ref} value={ref}>{ref.replace(/^refs\/(heads|tags)\//, '')} · {sha.slice(0, 12)}</option>)}
        </Select></Field>}
      </QueryState>
      {commit && <>
        <p className="break-all font-mono text-xs text-muted" translate="no">Commit {commit}</p>
        <QueryState query={refs}>{items => <Field label="Compare with published base"><Select value={compare} onChange={e => setCompare(e.target.value)}><option value="">No comparison</option>{Object.entries(items).map(([ref, sha]) => <option key={ref} value={sha}>{ref} · {sha.slice(0, 12)}</option>)}</Select></Field>}</QueryState>
        {compare && <QueryState query={diff}>{data => <pre role="region" aria-label="Published diff" tabIndex={0} className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-control border border-line p-3 font-mono text-xs">{data.diff || 'No changes between these published commits.'}</pre>}</QueryState>}
        <Field label="View published content"><Select value={view} onChange={e => { setView(e.target.value); setPath(''); }}>
          <option value="files">Files</option><option value="wiki">Wiki</option>
        </Select></Field>
        <QueryState query={tree}>{items => {
          const paths = view === 'wiki' ? items.filter(p => p.startsWith('docs/wiki/') && p.endsWith('.md')) : items;
          return paths.length ? <Field label="Published file"><Select value={path} onChange={e => setPath(e.target.value)}>
            <option value="">Choose file…</option>{paths.map(p => <option key={p} value={p}>{view === 'wiki' ? p.slice(10) : p}</option>)}
          </Select></Field> : <p className="text-sm text-muted">No published {view === 'wiki' ? 'Wiki pages' : 'regular files'} at this commit.</p>;
        }}</QueryState>
        {path && <QueryState query={file}>{data => <pre role="region" aria-label="Published text" tabIndex={0} className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-control border border-line bg-panel-2 p-3 font-mono text-xs">{data.content}</pre>}</QueryState>}
      </>}
    </div>
  </Dialog>;
}
