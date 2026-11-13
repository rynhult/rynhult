import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Pin } from 'lucide-react';
import { api } from '../../lib/api';
import { useAttention, useGroupAdministration, useInstance, useProjects, useRole, useSessions } from '../../lib/queries';
import { workspaceName } from '../../lib/format';
import { groupLive, sessionState } from '../session-bits';
const SettingsScopeLinks = lazy(() => lazyImport(() => import('./SettingsScopes')).then(m => ({ default: m.SettingsScopeLinks })));
import { announce, Button, IconButton, Input, QueryState } from '../ui';
import { lazyImport } from '../../lib/lazyImport';
const HostsSidebar = lazy(() => lazyImport(() => import('./Hosts')).then(m => ({ default: m.HostsSidebar })));
import { pathVisible, modeVisible } from '../../lib/setupLevels';
import { useSetupLevel } from '../../lib/setupPreference';
import { railFor, toolsFor, type Mode } from './navigation';
import { resolveProjects } from './presentation';
const InboxLinks = lazy(() => lazyImport(() => import('./InboxLinks')).then(m => ({ default: m.InboxLinks })));
import { useProjectSourceView } from '../../lib/sourceViews';
const SourceDirectorySummary = lazy(() => lazyImport(() => import('../SourceViews')).then(m => ({ default: m.SourceDirectorySummary })));

export function Sidebar({ mode, pins, recent, pin, visit, close }: { mode: Mode; pins: number[]; recent: number[]; pin: (id: number) => void; visit: (id: number) => void; close: () => void }) {
  const owner = useRole().owner;
  const level = useSetupLevel();
  const groupAdmin = useGroupAdministration();
  const instance = useInstance();
  const projects = useProjects();
  const sessions = useSessions();
  const attention = useAttention();
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const route = useLocation();
  const available = useMemo(() => (projects.data ?? []).filter(p => !p.archived)
    .sort((a, b) => a.slug.localeCompare(b.slug) || a.id - b.id), [projects.data]);
  const matching = useMemo(() => available.filter(p => `${p.name} ${p.slug} ${p.repo ?? ''}`.toLowerCase().includes(search.toLowerCase())), [available, search]);
  const current = Math.min(page, Math.max(0, Math.ceil(matching.length / 20) - 1));
  const visible = matching.slice(current * 20, (current + 1) * 20);
  const resultNotice = !matching.length ? (available.length ? 'No projects match.' : 'No projects yet.')
    : `Projects ${current * 20 + 1}–${Math.min((current + 1) * 20, matching.length)} of ${matching.length}`;
  const lastNotice = useRef('');
  useEffect(() => {
    if (mode !== 'projects' || !projects.isSuccess) return;
    const timer = window.setTimeout(() => {
      if (lastNotice.current !== resultNotice) { lastNotice.current = resultNotice; announce(resultNotice); }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [mode, projects.isSuccess, resultNotice]);
  const sessionId = route.pathname.match(/^\/sessions\/([^/]+)(?:\/|$)/)?.[1];
  const sessionProjectId = sessions.data?.find(s => s.id === sessionId)?.project_id;
  const project = available.find(p => p.id === sessionProjectId || route.pathname === `/projects/${p.id}` || route.pathname.startsWith(`/projects/${p.id}/`));
  const live = (sessions.data ?? []).filter(s => s.desired_state !== 'archived');
  // A session stays current on all of its tabs (?tab=files, ?tab=changes, …); other links match exactly.
  const link = (to: string, label: string, samePath = false) => <Link key={to} className="shell-link" aria-current={(samePath ? route.pathname : route.pathname + route.search + route.hash) === to ? 'page' : undefined} to={to} onClick={close}>{label}</Link>;
  const projectRow = (p: typeof available[number]) => <div className="shell-project" key={p.id}>
    <Link className="shell-link min-w-0 flex-1 text-left" to={`/projects/${p.id}`} onClick={() => { visit(p.id); close(); }}>
      <span className="block truncate">{p.name}</span><span className="block text-xs text-muted">{p.machine_offline ? 'Machine offline' : p.host === 'local' ? instance.host : p.host}{!p.repo && !p.remote_url ? ' · Local only' : ''}</span>
    </Link><IconButton label={`${pins.includes(p.id) ? 'Unpin' : 'Pin'} ${p.name}`} icon={Pin} onClick={() => pin(p.id)} />
  </div>;
  const selectedProject = mode === 'projects' ? project : undefined;
  const title = selectedProject?.name ?? (mode === 'hosts' ? 'Hosts' : mode[0].toUpperCase() + mode.slice(1));
  const stats = mode === 'inbox' && attention.data ? `${attention.data.length} open items shown` : selectedProject
    ? `${selectedProject.private_workspace ? 'Private workspace' : selectedProject.repo || selectedProject.remote_url ? 'Repository project' : 'Local project'}${selectedProject.import_mode === 'readonly' ? ' · Read-only' : ''} · ${selectedProject.host === 'local' ? instance.host : selectedProject.host}` : null;
  return <div className="shell-sidebar-content" role={selectedProject ? 'region' : undefined} aria-label={selectedProject ? `${selectedProject.name} context` : undefined}><header className="shell-sidebar-heading"><div>
    <h2 className="font-semibold" title={title}>{title}</h2>{stats && <p className="shell-sidebar-stats">{stats}</p>}
    </div>{selectedProject && <IconButton label={`${pins.includes(selectedProject.id) ? 'Unpin' : 'Pin'} ${selectedProject.name}`} icon={Pin} onClick={() => pin(selectedProject.id)} />}</header>
    {mode === 'projects' && <QueryState query={projects}>{() => project ? <section>
      {link('/projects', 'Back to all projects')}
      <h3 className="shell-caption">Work</h3>
      {link(`/projects/${project.id}`, 'Overview')}
      <QueryState query={sessions}>{() => link(`/projects/${project.id}#project-sessions`, `Sessions · ${live.filter(s => s.project_id === project.id).length}`)}</QueryState>
      {link(`/projects/${project.id}?tab=files`, 'Files')}{link(`/projects/${project.id}?tab=changes`, 'Changes')}
      {instance.features?.live_preview !== false && link(`/projects/${project.id}?tab=preview`, 'Preview')}
      <h3 className="shell-caption">Project</h3>
      <ProjectMenuSources projectId={project.id} close={close} />
      {link(`/projects/${project.id}#${instance.orchestrator ? 'project-brief' : 'project-knowledge'}`, instance.orchestrator ? 'Brief & knowledge' : 'Knowledge')}
      {link(`/projects/${project.id}/wiki`, 'Wiki')}
      {!project.private_workspace && link(`/projects/${project.id}#project-repository`, 'Issues & PRs')}
      <h3 className="shell-caption">Working locations</h3>
      <p className="px-2 text-sm break-words" title={project.workspace_path}>{project.host === 'local' ? instance.host : project.host} · {project.machine_offline ? 'Machine offline' : 'Available'}</p>
      {link(`/settings?project_id=${project.id}#project-settings`, 'Project settings')}
    </section> : <>
      <Input aria-label="Search projects" autoComplete="off" placeholder="Search projects…" value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} />
      <>
        {!search && pins.length > 0 && <section aria-label="Pinned projects"><h3 className="shell-caption">Pinned</h3>{resolveProjects(pins, available).map(projectRow)}</section>}
        {!search && recent.length > 0 && <section aria-label="Recent projects"><h3 className="shell-caption">Recent</h3>{resolveProjects(recent.filter(id => !pins.includes(id)), available).map(projectRow)}</section>}
        <section aria-label="Available projects"><h3 className="shell-caption">Available</h3>{visible.map(projectRow)}{!matching.length && <p className="text-muted">{available.length ? 'No projects match.' : 'No projects yet.'}</p>}
          {matching.length > 20 && <div className="flex flex-wrap items-center gap-2 text-xs">
            <span>Projects {current * 20 + 1}–{Math.min((current + 1) * 20, matching.length)} of {matching.length}</span>
            <Button size="sm" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous projects</Button>
            <Button size="sm" disabled={(current + 1) * 20 >= matching.length} onClick={() => setPage(current + 1)}>Next projects</Button>
          </div>}
        </section>
      </>
    </>}</QueryState>}
    {mode === 'sessions' && <><QueryState query={sessions}>{() => groupLive(live).map(group => <section key={group.key}>
      <h3>{link(`/sessions?group=${group.key}`, `${group.label} · ${group.count}`)}</h3>
      {group.sessions.map(s => s.management === 'external' && s.remote_url ? <a className="shell-link" key={s.id} href={s.remote_url} target="_blank" rel="noreferrer">{s.name} · Remote Control</a> : link(`/sessions/${s.id}`, `${s.name} · ${sessionState(s).label}`, true))}
    </section>)}</QueryState></>}
    {mode === 'inbox' && <Suspense fallback={null}><InboxLinks close={close} /></Suspense>}
    {mode === 'hosts' && <Suspense fallback={null}><HostsSidebar close={close} /></Suspense>}
    {mode === 'workspaces' && <Workspaces close={close} />}
    {mode === 'tools' && <>
      {toolsFor(owner, groupAdmin, instance.features, path => path !== '/catalog' && pathVisible(level, path)).map(n => link(n.to, n.label))}
      <details><summary className="shell-link cursor-pointer">More tools</summary>
        {railFor(owner, groupAdmin, instance.features).filter(n => !modeVisible(level, n.id)).map(n => link(n.to, n.label))}
        {toolsFor(owner, groupAdmin, instance.features, path => path !== '/catalog' && !pathVisible(level, path)).map(n => link(n.to, n.label))}
        <Link className="shell-link" to="/settings#security" onClick={close}>Security & recovery settings</Link>
      </details>
    </>}
    {mode === 'settings' && <><Suspense fallback={null}><SettingsScopeLinks compact close={close}/></Suspense></>}

  </div>;
}
function Workspaces({ close }: { close: () => void }) {
  const query = useQuery({ queryKey: ['workspaces'], queryFn: () => api<{ folder: string; project: { id: number; name: string } | null }[]>('/api/workspaces'), refetchInterval: 10000 });
  return <QueryState query={query}>{rows => rows.length ? rows.map(w => <Link className="shell-link" key={w.folder} onClick={close} to={w.project ? `/projects/${w.project.id}` : `/workspaces/${encodeURIComponent(w.folder)}/files`}>{w.project?.name ?? workspaceName(w.folder)}</Link>) : <p className="text-muted">No private workspaces yet.</p>}</QueryState>;
}

/** Same audience-scoped P4 read as the project pages; no new source authority. */
function ProjectMenuSources({ projectId, close }: { projectId: number; close: () => void }) {
  const query = useProjectSourceView(projectId);
  return <div className="shell-project-sources"><QueryState query={query} errorTitle="Couldn't load project sources.">
    {view => !view.repositories.length ? null : <><Suspense fallback={null}><SourceDirectorySummary view={view} kind="repositories" /></Suspense>
      <Link className="shell-link" to={`/projects/${projectId}#project-sources`} onClick={close}>View source repositories</Link>
    </>}
  </QueryState></div>;
}
