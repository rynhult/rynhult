/** Unsaved drafts kept for this browser tab (sessionStorage): a refresh or Back restores what the person was typing,
 *  instead of dropping it (break-it review 2026-10-26). Per tab, so two tabs never overwrite each other's draft, and
 *  gone when the tab closes. Storage can be unavailable (private windows, blocked site data): then drafts simply aren't
 *  kept, as before. */
const PREFIX = 'acc-draft:';

export function loadDraft<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(PREFIX + key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

export function saveDraft(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(PREFIX + key, JSON.stringify(value));
    window.dispatchEvent(new Event('acc-draft-change'));
  } catch {
    // Storage full or unavailable: the draft lives only in the page, as before.
  }
}

export function clearDraft(key: string): void {
  try {
    sessionStorage.removeItem(PREFIX + key);
    window.dispatchEvent(new Event('acc-draft-change'));
  } catch {
    // Nothing kept, nothing to clear.
  }
}
