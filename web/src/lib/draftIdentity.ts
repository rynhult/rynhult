/** Auth boundary shared without loading draft editors on the sign-in page. */
export interface DraftScope { installation: string; person: string; authority?: string }
let current: DraftScope | null = null;
const listeners = new Set<(scope: DraftScope | null) => void>();
export const draftIdentity = () => current;
export function setDraftIdentity(scope: DraftScope | null) {
  if (JSON.stringify(current) === JSON.stringify(scope)) return;
  current = scope;
  listeners.forEach(listener => listener(scope));
}
export function onDraftIdentity(listener: (scope: DraftScope | null) => void) { listeners.add(listener); }
