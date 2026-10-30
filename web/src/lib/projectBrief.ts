import { useSyncExternalStore } from 'react';
import { clearDraft, loadDraft, saveDraft } from './drafts';

// The open form must still reach launch consent when browser storage is full or blocked.
const liveDrafts = new Map<number, BriefDraft>();
export function readBriefDraft(projectId: number) {
  return liveDrafts.get(projectId) ?? loadDraft<BriefDraft>(`brief:${projectId}`);
}
export function keepBriefDraft(projectId: number, draft: BriefDraft | null) {
  if (draft) { liveDrafts.set(projectId, draft); saveDraft(`brief:${projectId}`, draft); }
  else { liveDrafts.delete(projectId); clearDraft(`brief:${projectId}`); }
  window.dispatchEvent(new Event('acc-brief-draft-change'));
}

export function useBriefDraft(projectId: number) {
  return useSyncExternalStore(listener => {
    window.addEventListener('acc-brief-draft-change', listener);
    return () => window.removeEventListener('acc-brief-draft-change', listener);
  }, () => !!readBriefDraft(projectId));
}

export const labels = { product: 'Product or feature', people_problem: 'People and problem', journeys: 'Main journeys',
  first_release: 'First release', later: 'Exclusions and later ideas', success: 'Success', design: 'Design direction',
  constraints: 'Constraints', uncertainty: 'Uncertainty' };
export type Fields = Record<string, string>;
export interface Reference { reference_id: string; caption: string; guidance: string; url: string | null; media_type: string | null; size: number; document?: { status: string; error?: string; truncated?: boolean } }
export interface Version { version: number; base_version: number; submitted: Fields; references: Reference[]; state: string;
  conflicts: string[]; actor: string; markdown: string; source?: string; citations?: Record<string, { reference: string; location: string }[]> }
export interface BriefView { pending_proposal?: Version | null; latest_proposal?: Version | null; review_proposal?: Version | null; current_version: number; next_version: number; current: Version | null; history: Version[]; export_path: string; documents?: Reference[] }
export interface BriefDraft { draft: Fields; references: Reference[]; base: number; exportPath?: string }
export interface SavedBriefEvent { projectId: number; draft: BriefDraft; saved: Version }
