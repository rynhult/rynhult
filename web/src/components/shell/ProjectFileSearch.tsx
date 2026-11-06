import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { navigationIndex } from '../../lib/keyboard';
import { useInstance, useProjects } from '../../lib/queries';
import type { Project } from '../../lib/types';
import { announce, Button, Dialog, ErrorNote, Field, Input, Select } from '../ui';

type SearchResults = { results: { name: string; path: string; kind: 'file' }[]; visited: number; truncated: boolean; reason: string | null };
const partialReasons: Record<string, string> = {
  entry_limit: 'The folder is too large to search completely.', result_limit: 'The result limit was reached.',
  time_limit: 'The search time limit was reached.', depth_limit: 'Some folders are too deeply nested.',
  ignore_limit: 'Some ignore rules could not be read.', path_limit: 'Some paths are too long.',
  unreadable: 'Some folders could not be read.', mount_boundary: 'A separate filesystem was skipped.',
  unsupported_name: 'Some filenames could not be searched.',
};

// Consuming the query signal cancels both the debounce and the request on typing, target change or close.
function debounce(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 300);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export function ProjectFileSearch({ children }: { children: (show: () => void) => ReactNode }) {
  const projects = useProjects(); const instance = useInstance();
  const route = useLocation();
  const routeProject = /^\/projects\/(\d+)(?:\/|$)/.exec(route.pathname);
  const currentProjectId = routeProject ? Number(routeProject[1]) : null;
  const [open, setOpen] = useState(false);
  // Browsing the sidebar and later route changes must never retarget an existing search.
  const initialized = useRef(false); const [targetId, setTargetId] = useState<number | null>(null);
  const target = projects.data?.find(p => p.id === targetId);
  const show = useCallback(() => {
    if (!initialized.current) { initialized.current = true; setTargetId(currentProjectId); }
    setOpen(true);
  }, [currentProjectId]);
  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || e.keyCode === 229 || e.altKey || document.querySelector('[role=dialog]')) return;
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f') { e.preventDefault(); show(); }
    };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, [show]);
  return <>
    {children(show)}
    <Dialog open={open} onClose={() => setOpen(false)} title="Find project files">
      {open && <div className="space-y-4">
        <Field label="Search project"><Select name="search-project" value={target?.id ?? ''} onChange={e => setTargetId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Choose a project</option>
          {projects.data?.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select></Field>
        {projects.isError && <ErrorNote error={projects.error} />}
        {target ? <p className="break-words [overflow-wrap:anywhere]">Searching <strong>{target.name}</strong> · {target.host === 'local' ? instance.host : target.host}{target.account ? ` · ${target.account}` : ''}<br />Primary project folder: <code>{target.workspace_path}</code></p>
          : <p>{projects.isPending ? 'Loading projects…' : targetId ? 'The selected project is no longer available. Choose a project to search.' : 'Choose a project to search its primary folder.'}</p>}
        <p className="text-muted">Filenames and paths only, never file contents. Session worktrees are not searched. Up to 20 results; refine the filename to narrow your search.</p>
        <Search key={`${target?.id ?? ''}:${!!target?.machine_offline}`} project={target} close={() => setOpen(false)} />
      </div>}
    </Dialog>
  </>;
}

function Search({ project, close }: { project?: Project; close: () => void }) {
  const [value, setValue] = useState(''); const q = value.trim(); const list = useRef<HTMLUListElement>(null);
  const search = useQuery({
    queryKey: ['shell-project-file-search', project?.id, q], enabled: !!project && !project.machine_offline && !!q,
    queryFn: async ({ signal }) => {
      await debounce(signal);
      return api<SearchResults>(`/api/projects/${project!.id}/files/search?q=${encodeURIComponent(q)}&limit=20`, { signal, cache: 'no-store' });
    },
    retry: false, gcTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
  const unavailable = search.error instanceof ApiError && search.error.status === 503 && (search.error.data as { state?: string } | undefined)?.state === 'unavailable';
  const data = search.data;
  const status = !project ? '' : project.machine_offline ? 'Machine offline. Wait for the project’s machine to reconnect, then reopen search.'
    : !q ? 'Enter a filename. Use ↑ and ↓ to move through results, and Enter to open a file.'
    : search.isPaused ? 'Search paused. Your browser is offline; reconnect to search this project.'
    : search.isPending || search.isFetching ? 'Searching filenames…'
    : search.isError ? unavailable ? `Search unavailable. ${search.error.message}` : 'Search failed. Check the error below and try again.'
    : data?.truncated ? `Partial results. ${partialReasons[data.reason ?? ''] ?? 'Some of the folder could not be searched.'} Refine your search.${data.results.length ? '' : ' No matches in the searched portion; other files may match.'}`
    : data?.results.length ? `${data.results.length} ${data.results.length === 1 ? 'file' : 'files'} found.` : 'No filenames match. Try another filename.';
  useEffect(() => { if (status && project) announce(`${project.name}: ${status}`); }, [project?.name, status]);
  const keys = (e: KeyboardEvent<HTMLElement>) => {
    if (e.nativeEvent.isComposing || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const links = [...(list.current?.querySelectorAll<HTMLAnchorElement>('a') ?? [])];
    const fromInput = e.target instanceof HTMLInputElement;
    if (fromInput && e.key === 'Enter' && links[0]) { e.preventDefault(); links[0].click(); return; }
    const next = navigationIndex(e.key, links.indexOf(e.target as HTMLAnchorElement), links.length, { orientation: 'vertical', homeEnd: !fromInput });
    if (next !== null) { e.preventDefault(); links[next]?.focus(); }
  };
  return <div className="space-y-3" onKeyDown={keys}>
    <Field label="Filename"><Input type="search" name="filename" autoComplete="off" spellCheck={false} maxLength={200} disabled={!project || project.machine_offline} value={value} onChange={e => setValue(e.target.value)} /></Field>
    {status && <p>{status}</p>}
    {search.isError && !unavailable && <ErrorNote error={search.error} />}
    {search.isError && <Button onClick={() => void search.refetch()}>Try search again</Button>}
    {!!q && !search.isPaused && !search.isFetching && !search.isError && !!data?.results.length && <ul ref={list} aria-label="Matching files" className="space-y-1">
      {data.results.map(file => <li key={file.path}><Link className="shell-link min-h-11" to={`/projects/${project!.id}?tab=files&file=${encodeURIComponent(file.path)}`} onClick={e => { if (e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) close(); }}>{file.path}</Link></li>)}
    </ul>}
  </div>;
}
