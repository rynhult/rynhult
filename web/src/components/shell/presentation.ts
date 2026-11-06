/** The browser origin identifies this installation; the API supplies the stable signed-in user id.
 * No username, project name, path, draft or permission is cached here. */
export function localKey(installation: string, person: string | null, field: string) {
  return person === null ? null : `rynhult:shell:v1:${encodeURIComponent(installation)}:${person}:${field}`;
}
export function projectIds(value: unknown): number[] {
  return Array.isArray(value) ? [...new Set(value.filter((x): x is number => Number.isSafeInteger(x) && x > 0))].slice(0, 100) : [];
}
export function resolveProjects<T extends { id: number }>(ids: number[], authorized: T[]): T[] {
  return ids.flatMap(id => { const p = authorized.find(p => p.id === id); return p ? [p] : []; });
}
