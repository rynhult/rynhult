import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigationType, useSearchParams } from 'react-router-dom';
import { FolderGit2, FolderPlus, Plus } from 'lucide-react';
import { useProjects, useSessions } from '../../lib/queries';
import { useSourceDirectory } from '../../lib/sourceViews';
import { SourceDirectoryFilters, SourceDirectorySummary } from '../SourceViews';
import { needsYou } from '../../lib/order';
import { isLive } from '../session-bits';
import type { Project } from '../../lib/types';
import { Badge, Button, Empty, Field, Input, Select, QueryState } from '../ui';
import { ShellPage as Page, Workbar as PageHeader } from './PagePrimitives';
import './ProjectPages.css';

export function ProjectDirectory({ onCreate, onRegister, actions }: { onCreate: () => void; onRegister: () => void; actions: (project: Project) => ReactNode }) {
  const projects = useProjects(); const sessions = useSessions();
  const [search, setSearch] = useSearchParams();
  const navigationType = useNavigationType();
  const pendingFilter = useRef<string | null>(null);
  // Native inputs update synchronously; URL navigation may commit in a transition.
  const [filters, setFilters] = useState(search);
  useEffect(() => {
    // Ignore older acknowledgements of our own replace navigations while typing.
    // Back/forward and external links still replace the local filters.
    if (navigationType === 'REPLACE' && pendingFilter.current !== null && search.toString() !== pendingFilter.current) return;
    pendingFilter.current = null; setFilters(search);
  }, [search, navigationType]);
  const query = filters.get('q') ?? '';
  const cards = filters.get('view') === 'cards';
  const columns = [['name', 'Name'], ['status', 'Status'], ['activity', 'Last activity'], ['runtime', 'Runtime']] as const;
  const sort = columns.some(([key]) => key === filters.get('sort')) ? filters.get('sort')! : 'name';
  const direction = filters.get('direction') === 'desc' ? 'desc' : 'asc';
  function sorting(key: string, order: string) {
    const next = new URLSearchParams(filters); next.set('sort', key); next.set('direction', order); next.delete('offset'); update(next);
  }
  const archived = filters.get('archived') === '1';
  const offset = Math.max(0, Math.min(100000, Number(filters.get('offset')) || 0));
  const value = { q: query, source_owner: filters.get('source_owner') ?? '', host_id: filters.get('host_id') ?? '',
    offset, limit: 20, sort, direction, projects_only: true, include_archived: archived };
  const directory = useSourceDirectory(value);
  // A saved page can disappear after archive/removal. Clamp only after the
  // current authorized response, preserving every other URL filter.
  useEffect(() => {
    const data = directory.data;
    if (!data || directory.isFetching || data.offset === 0 || data.offset < data.total) return;
    const next = new URLSearchParams(filters);
    next.set('offset', String(Math.max(0, Math.ceil(data.total / data.limit) - 1) * data.limit));
    update(next);
  }, [directory.data, directory.isFetching, filters, setSearch]);
  function update(next: URLSearchParams) { pendingFilter.current = next.toString(); setFilters(next); setSearch(next, { replace: true }); }
  function filter(key: string, value: string) {
    const next = new URLSearchParams(filters); if (value) next.set(key, value); else next.delete(key);
    next.delete('offset'); update(next);
  }
  function paginate(offset: number) { const next = new URLSearchParams(filters); next.set('offset', String(offset)); update(next); }
  return <Page wide className="shell-project-page">
    <PageHeader title="All projects" meta={<span>Projects / Directory</span>} actions={<>
      <Button onClick={onRegister}><Plus size={14} aria-hidden="true" /> Register folder</Button>
      <Button variant="primary" onClick={onCreate}><FolderPlus size={14} aria-hidden="true" /> New project</Button>
    </>} />
    <section aria-label="Project filters" className="shell-directory-filters">
      <Field label="Find a project or repository"><Input name="project-search" autoComplete="off" value={query} onChange={e => filter('q', e.target.value)} placeholder="Search projects, owners or repositories…" /></Field>
      {<SourceDirectoryFilters data={directory.data ?? { items: [], total: 0, offset: 0, limit: 20, next_offset: null, facets: { source_owners: [], hosts: [], scope: 'visible_page' } }} value={value} onChange={next => {
        const params = new URLSearchParams(filters);
        for (const key of ['source_owner', 'host_id'] as const) { if (next[key]) params.set(key, next[key]); else params.delete(key); }
        params.delete('offset'); update(params);
      }} />}
      <label className="shell-directory-archived"><input type="checkbox" checked={archived} onChange={e => filter('archived', e.target.checked ? '1' : '')} /> Show archived</label>
    </section>
    <QueryState query={directory} errorTitle="Couldn't load project sources and locations.">{data => <>
      <div className="shell-directory-count"><div role="group" aria-label="Project view"><Button aria-pressed={!cards} onClick={() => filter('view', '')}>List</Button><Button aria-pressed={cards} onClick={() => filter('view', 'cards')}>Cards</Button></div><p>{data.total} project{data.total === 1 ? '' : 's'}</p></div>
      {!data.items.length ? <Empty icon={FolderGit2}>{query || value.host_id || value.source_owner ? 'No projects match these filters.' : 'No projects yet. Start one from an empty folder or a repository.'}</Empty> : <>
        {!cards && <label className="shell-project-phone-sort">Sort projects<Select value={`${sort}:${direction}`} onChange={event => { const [key, order] = event.target.value.split(':'); sorting(key, order); }}>{columns.flatMap(([key, label]) => ['asc', 'desc'].map(order => <option key={`${key}:${order}`} value={`${key}:${order}`}>{label} · {order === 'asc' ? 'ascending' : 'descending'}</option>))}</Select></label>}
        <ProjectTableViewport>
          <table aria-label="Project directory" className={`shell-directory-table ${cards ? 'shell-directory-cards' : 'shell-project-list'}`}>
            <thead><tr>{cards ? ['Project', 'Source repositories', 'Working locations', 'Activity'].map(name => <th key={name} scope="col">{name}</th>) : columns.map(([key, label]) => <th key={key} scope="col" aria-sort={sort === key ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}><button type="button" onClick={() => sorting(key, sort === key && direction === 'asc' ? 'desc' : 'asc')}>{label}{sort === key ? direction === 'asc' ? ' ↑' : ' ↓' : ''}</button></th>)}</tr></thead>
            <tbody>{data.items.map(view => {
              const p = projects.error ? undefined : projects.data?.find(p => p.id === view.id);
              const live = (sessions.data ?? []).filter(s => s.project_id === view.id && isLive(s)); const waiting = needsYou(live).length;
              return <tr key={view.id}>
                <th scope="row"><Link to={`/projects/${view.id}`}>{view.name}</Link>{p?.notes && <p className="shell-project-description">{p.notes}</p>}<div className="shell-project-flags">{p?.archived && <Badge>Archived</Badge>}{p?.import_mode === 'readonly' ? <Badge tone="wait">Read-only</Badge> : p && !p.trusted && <Badge tone="wait">Not trusted</Badge>}</div></th>
                {cards ? <><td><SourceDirectorySummary view={view} kind="repositories" /></td><td><SourceDirectorySummary view={view} kind="locations" /></td></> : <><td data-label="Status">{view.status ?? (p?.archived ? 'Archived' : p?.import_mode === 'readonly' ? 'Read-only' : p && !p.trusted ? 'Not trusted' : 'Ready')}<div>{p?.machine_offline ? 'Host offline' : sessions.isError ? 'Activity unavailable' : waiting ? `${waiting} ${waiting === 1 ? 'needs' : 'need'} you` : live.length ? `${live.length} active` : 'Idle'}</div></td><td data-label="Last activity">{view.last_activity ? <time dateTime={view.last_activity}>{new Date(view.last_activity).toLocaleString()}</time> : 'No activity'}</td></>}
                <td data-label="Runtime"><div className="shell-directory-activity">{!cards && <span title="Compose autostart configuration; open the project for live stack details.">{view.runtime ?? (p?.compose_autostart ? 'Autostart on' : 'Autostart off')}</span>}{cards && <span>{p?.machine_offline ? <Badge tone="wait">Host offline</Badge> : sessions.isError ? 'Activity unavailable' : !sessions.data ? 'Checking…' : waiting ? <Badge tone="wait">{waiting} {waiting === 1 ? 'needs' : 'need'} you</Badge> : live.length ? <span className="text-ok">{live.length} active</span> : 'Idle'}</span>}{p ? actions(p) : <span>Project actions unavailable</span>}</div></td>
              </tr>;
            })}</tbody>
          </table>
        </ProjectTableViewport>
        <nav aria-label="Project pages" className="shell-project-pagination"><span>Page {Math.floor(data.offset / data.limit) + 1} of {Math.max(1, Math.ceil(data.total / data.limit))}</span><div><Button disabled={!data.offset} onClick={() => paginate(Math.max(0, data.offset - data.limit))}>Previous</Button><Button disabled={data.next_offset === null} onClick={() => paginate(data.next_offset!)}>Next</Button></div></nav>
      </>}
    </>}</QueryState>
  </Page>;
}


/** Menus may overflow a fitted table. Only genuinely wide tables become scroll boxes. */
function ProjectTableViewport({ children }: { children: ReactNode }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [horizontal, setHorizontal] = useState(false);
  useLayoutEffect(() => {
    const box = viewport.current;
    const table = box?.querySelector('table');
    if (!box || !table) return;
    const measure = () => setHorizontal(table.getBoundingClientRect().width > box.clientWidth + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box); observer.observe(table);
    return () => observer.disconnect();
  }, []);
  return <div ref={viewport} className="shell-directory-scroll" data-overflow={horizontal ? 'horizontal' : 'visible'}
    role="region" aria-label="Projects table, scroll horizontally" tabIndex={0}>{children}</div>;
}
