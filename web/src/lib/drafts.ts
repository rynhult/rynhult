/** Best-effort, person/installation-scoped tab storage. A successful local write
 * is not a server save or a backup. Failed writes retain the current memory copy. */
import { unsavedWork, type DraftKind } from './unsaved.ts';
const PREFIX = 'acc-draft:v2:';
// Evidence of an on-disk copy survives a later blocked read. Scoped keys keep
// this evidence separate across people and authority changes.
const storedCopies = new Set<string>();
function storageKey(key: string) {
  const scope = unsavedWork.getScope();
  return scope ? PREFIX + JSON.stringify([scope.installation, scope.person, scope.authority ?? '', key]) : null;
}
const families: Record<string, [DraftKind, string]> = {
  brief: ['brief', 'Project brief'], plan: ['plan', 'Team plan'], now: ['worklist', 'Working list'],
  answer: ['worklist', 'Working list reply'], discuss: ['worklist', 'Message to lead'],
  'import-folder': ['register-folder', 'Register a folder'], 'worklist-add': ['worklist', 'New working list item'],
};
export function loadDraft<T>(key: string): T | null {
  const live = unsavedWork.read<T>(key);
  if (live !== undefined) return live;
  const scoped = storageKey(key);
  if (!scoped) return null;
  try { const raw = sessionStorage.getItem(scoped); if (raw !== null) storedCopies.add(scoped); return raw ? JSON.parse(raw) as T : null; }
  catch { return null; }
}
export function saveDraft(key: string, value: unknown, discard?: () => void): boolean {
  const scoped = storageKey(key);
  let saved = false;
  if (scoped) {
    try { sessionStorage.setItem(scoped, JSON.stringify(value)); storedCopies.add(scoped); saved = true; } catch { /* retained below */ }
  }
  const [kind, label] = families[key.split(':')[0]] ?? ['worklist', 'Unsaved draft'];
  const entry = { id: key, kind, label, value, persistence: saved ? 'local' as const : 'failed' as const,
    discard: () => { clearDraft(key); discard?.(); }, flush: () => saveDraft(key, value, discard) };
  if (scoped) unsavedWork.register(entry); else unsavedWork.registerVolatile(entry);
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('acc-draft-change'));
  return saved;
}
export function clearDraft(key: string): boolean {
  const scoped = storageKey(key);
  if (scoped) {
    let stored = storedCopies.has(scoped);
    try { stored = sessionStorage.getItem(scoped) !== null; } catch { /* use known copy evidence */ }
    try { sessionStorage.removeItem(scoped); storedCopies.delete(scoped); }
    catch {
      // Clean mounts also call clearDraft. A refused removal alone is not
      // evidence that an older copy exists and must not invent unsaved work.
      if (!stored) { unsavedWork.unregister(key); return true; }
      const [kind, label] = families[key.split(':')[0]] ?? ['worklist', 'Unsaved draft'];
      // A tombstone prevents same-page resurrection; keep the guard because
      // reload may still recover the older disk copy until removal succeeds.
      unsavedWork.register({ id: key, kind, label, value: null, persistence: 'failed', problem: 'remove',
        discard: () => { clearDraft(key); }, flush: () => clearDraft(key) });
      return false;
    }
  }
  unsavedWork.unregister(key);
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('acc-draft-change'));
  return true;
}
