/** Read-only P4 views shared by project sections, Astra's menu and the directory.
 * Opaque membership/location IDs are presentation keys, never launch targets.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import { useRole } from './queries';

export interface SourceRow {
  membership_id: string; membership_ids: string[]; repository_id: string | null; locator_id: string | null;
  locator: string | null; verification: string; name: string | null;
  owner: { id: string; login: string | null } | null;
  instance: { id: string; provider: string; api_origin: string } | null;
  compatibility_primary: boolean;
}
export interface LocationRow {
  id: string; membership_id: string | null; location_id: string; registration_id: string;
  host: string; host_id: string; account: string; account_id: string; path: string;
  trust: string; sync_status: string; verification: string; compatibility_default: boolean;
  host_status: string; last_seen_at: string | null;
}
export interface ProjectSourceView {
  audience_mismatch: boolean;
  project_id: number; repositories: SourceRow[]; locations: LocationRow[];
  membership_count: number; registration_count: number;
  counts: { repositories: number; locations: number }; counts_scope: 'visible'; read_only: true; truncated: { repositories: boolean; locations: boolean };
}
export interface SourceDirectory {
  items: (ProjectSourceView & { id: number; name: string; slug: string; status?: string; last_activity?: string | null; runtime?: string })[];
  total: number; offset: number; limit: number; next_offset: number | null;
  facets: { source_owners: { id: string; login: string | null; instance: SourceRow['instance'] }[];
    hosts: { id: string; name: string }[]; scope: 'visible_page' };
}
export interface DirectoryFilters { q?: string; source_owner?: string; host_id?: string; offset?: number; limit?: number; projects_only?: boolean; include_archived?: boolean; sort?: string; direction?: string }

export function useProjectSourceView(projectId: number, groupId?: string, enabled = true) {
   const { username, role, account } = useRole();
  const query = useQuery({ queryKey: ['source-view', username, role, account, groupId ?? null, projectId],
    queryFn: () => api<ProjectSourceView>(`/api/source-views/${groupId ? `groups/${encodeURIComponent(groupId)}/` : ''}projects/${projectId}`, { cache: 'no-store' }),
    enabled: !!username && enabled, staleTime: 0, refetchInterval: 30000, retry: false });
  // Authority errors must never render a formerly authorized cached placement.
  return { ...query, data: query.error || false || !enabled ? undefined : query.data };
}
export function useSourceDirectory(filters: DirectoryFilters, enabled = true) {
   const { username, role, account } = useRole();
  const params = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]));
  const query = useQuery({ queryKey: ['source-directory', username, role, account, params.toString()],
    queryFn: () => api<SourceDirectory>(`/api/source-views/directory?${params}`, { cache: 'no-store' }),
    enabled: !!username && enabled, staleTime: 0, refetchInterval: 30000, retry: false });
  return { ...query, data: query.error || false || !enabled ? undefined : query.data };
}
