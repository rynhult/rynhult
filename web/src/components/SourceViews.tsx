import { useId } from 'react';
import type { ReactNode } from 'react';
import { useNewShell } from '../lib/uiShell';
import { useProjectSourceView, type DirectoryFilters, type LocationRow, type ProjectSourceView, type SourceDirectory, type SourceRow } from '../lib/sourceViews';
import { ago } from '../lib/format';
import { Badge, Button, Field, QueryState, Select } from './ui';

function Surface({ title, children, meta }: { title: string; children: ReactNode; meta?: ReactNode }) {
  const id = useId();
  return <section aria-labelledby={id} className="min-w-0 overflow-hidden rounded-md border border-line bg-panel [overflow-wrap:anywhere]">
    <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-5 py-3"><h2 id={id} className="text-[15px] font-semibold">{title}</h2>{meta && <span className="text-xs text-muted">{meta}</span>}</header>{children}
  </section>;
}
export function SourceIdentity({ source }: { source: SourceRow }) {
  return <div className="min-w-0 [overflow-wrap:anywhere]">
    <p className="font-medium">{source.instance ? `${source.owner?.login ?? 'Unknown owner'}/${source.name ?? 'Unnamed repository'}` : source.locator ?? 'Recorded source'}</p>
    <p className="text-xs text-muted">{source.instance ? `${source.instance.provider} · ${source.instance.api_origin}` : source.repository_id ? 'Repository details are not visible to you.' : 'Unresolved locator · repository identity has not been verified'}</p>
    {source.compatibility_primary && <span className="text-xs text-muted">Existing primary source</span>}
  </div>;
}
function LocationIdentity({ location: row }: { location: LocationRow }) {
  return <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
    <p className="font-mono text-[13px]">{row.path}</p>
    <p className="text-xs text-muted">{row.host} · {row.account} · {row.verification} · {row.trust}</p>
    <p className="text-xs text-muted">{row.host_status === 'local' ? 'This server' : row.host_status === 'not_checked' ? 'Machine availability not checked' : row.host_status}
      {row.last_seen_at && ` · last seen ${ago(row.last_seen_at)}`} · {row.sync_status}</p>
    <p className="text-xs text-muted">Recorded location · {row.location_id.slice(0, 8)} · registration {row.registration_id.slice(0, 8)}</p>
    {row.compatibility_default && <Badge>Existing launch location</Badge>}
  </div>;
}
const audienceNotice = 'Sources and working locations are recorded under another audience. Their details remain hidden; this view does not change the existing launch configuration.';
const unavailable = 'Connections are not available in this read-only view. Adding or unlinking repositories and changing defaults need a separate reviewed release.';
const targeting = 'Location preparation and choosing a different launch target are not available yet. Existing single-source launches keep their current checks and target.';

export function ProjectSources({ projectId, groupId }: { projectId: number; groupId?: string }) {
  const query = useProjectSourceView(projectId, groupId);
  const shell = useNewShell(); if (!shell) return null;
  return <Surface title="Sources" meta={query.data && `Visible repositories: ${query.data.counts.repositories} · memberships: ${query.data.membership_count}`}>
    <QueryState query={query} errorTitle="Couldn't read sources and working locations.">{view => <>
      {view.audience_mismatch && <p role="status" className="px-5 py-3 text-[13px] text-muted">{audienceNotice}</p>}
      {view.truncated.repositories && <p role="status" className="px-5 py-3 text-xs text-muted">Showing the first 100 source memberships. Counts include all visible memberships.</p>}
      {view.truncated.locations && <p role="status" className="px-5 py-3 text-xs text-muted">Showing the first 100 working locations. Counts include all visible registrations.</p>}
      {view.repositories.length ? <ul className="divide-y divide-line">{view.repositories.map(source => <li key={source.membership_id} className="px-5 py-3 space-y-3">
        <SourceIdentity source={source} />
        {view.locations.filter(row => row.membership_id !== null && source.membership_ids.includes(row.membership_id)).map(row => <div key={row.id} className="border-l-2 border-line pl-4"><LocationIdentity location={row} /></div>)}
        {!view.locations.some(row => row.membership_id !== null && source.membership_ids.includes(row.membership_id)) && <p className="text-xs text-muted">No visible working locations for this source.</p>}
      </li>)}</ul> : !view.audience_mismatch && <p className="px-5 py-4 text-[13px] text-muted">No repository source is visible. Local-only projects can work without one.</p>}
      <div className="border-t border-line px-5 py-3 space-y-2"><p className="text-xs text-muted">{unavailable}</p><div className="flex flex-wrap gap-2"><Button disabled>Add repository</Button><Button disabled>Manage connections</Button><Button disabled>Unlink source</Button></div></div>
    </>}</QueryState>
  </Surface>;
}
export function ProjectWorkingLocations({ projectId, groupId }: { projectId: number; groupId?: string }) {
  const query = useProjectSourceView(projectId, groupId);
  const shell = useNewShell(); if (!shell) return null;
  return <Surface title="Working locations" meta={query.data && `Visible working locations: ${query.data.counts.locations} · registrations: ${query.data.registration_count}`}>
    <QueryState query={query} errorTitle="Couldn't read sources and working locations.">{view => <>
      {view.audience_mismatch && <p role="status" className="px-5 py-3 text-[13px] text-muted">{audienceNotice}</p>}
      {view.truncated.locations && <p role="status" className="px-5 py-3 text-xs text-muted">Showing the first 100 working locations. Counts include all visible registrations.</p>}
      {view.locations.length ? <ul className="divide-y divide-line">{view.locations.map(row => <li key={row.id} className="px-5 py-3"><LocationIdentity location={row} /></li>)}</ul>
        : !view.audience_mismatch && <p className="px-5 py-4 text-[13px] text-muted">No working locations are visible to you. Group viewers cannot see private checkouts.</p>}
      <div className="border-t border-line px-5 py-3 space-y-2"><p className="text-xs text-muted">{targeting}</p><div className="flex flex-wrap gap-2"><Button disabled>Prepare location</Button><Button disabled>Change launch target</Button><Button disabled>Remove location</Button></div></div>
    </>}</QueryState>
  </Surface>;
}
/** Directory integration: parent owns URL/search/pagination state. Do not reset IDs on refresh.
 * Facets are intentionally page-visible; retain selected options outside the current page.
 */
export function SourceDirectoryFilters({ data, value, onChange }: { data: SourceDirectory; value: DirectoryFilters; onChange: (value: DirectoryFilters) => void }) {
  const shell = useNewShell(); if (!shell) return null;
  return <fieldset className="flex min-w-0 flex-wrap gap-3"><legend className="sr-only">Filter project sources and working locations</legend>
    <Field label="Source owner"><Select value={value.source_owner ?? ''} onChange={e => onChange({ ...value, source_owner: e.target.value, offset: 0 })}>
      <option value="">All source owners</option>{value.source_owner && !data.facets.source_owners.some(row => row.id === value.source_owner) && <option value={value.source_owner}>Selected source owner</option>}
      {data.facets.source_owners.map(row => <option key={row.id} value={row.id}>{row.login ?? 'Unnamed owner'} · {row.instance?.api_origin}</option>)}
    </Select></Field>
    <Field label="Working location machine"><Select value={value.host_id ?? ''} onChange={e => onChange({ ...value, host_id: e.target.value, offset: 0 })}>
      <option value="">All machines</option>{value.host_id && !data.facets.hosts.some(row => row.id === value.host_id) && <option value={value.host_id}>Selected machine</option>}
      {data.facets.hosts.map(row => <option key={row.id} value={row.id}>{row.name} · {row.id.slice(0, 8)}</option>)}
    </Select></Field><p className="text-xs text-muted">Options from visible projects on this page.</p>
  </fieldset>;
}
/** Render inside the directory's repository/location cells; counts never describe hidden placements. */
export function SourceDirectorySummary({ view, kind }: { view: ProjectSourceView; kind: 'repositories' | 'locations' }) {
  const shell = useNewShell(); if (!shell) return null;
  return <div className="min-w-0 text-[13px] [overflow-wrap:anywhere]"><p>{view.counts[kind]} visible {kind === 'repositories' ? 'repositories' : 'working locations'} · {kind === 'repositories' ? `${view.membership_count} memberships` : `${view.registration_count} registrations`}</p>
    {view.audience_mismatch && <p className="text-xs text-muted">{audienceNotice}</p>}
    {kind === 'repositories' ? view.repositories.slice(0, 3).map(row => <SourceIdentity key={row.membership_id} source={row} />) : view.locations.slice(0, 3).map(row => <p key={row.id} className="text-xs text-muted">{row.host} · {row.account} · {row.path} · {row.location_id.slice(0, 8)}</p>)}
    {(view.truncated[kind] || (kind === 'repositories' ? view.repositories.length : view.locations.length) > 3) && <p className="text-xs text-muted">Showing up to 3 entries. Open the project for its visible sources and registrations.</p>}
  </div>;
}
