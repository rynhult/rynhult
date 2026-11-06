import { useEffect, useRef, useSyncExternalStore } from 'react';
import { unsavedWork, type DraftKind } from './unsaved';

/** The shell can inspect this list without receiving any draft content. */
export function useUnsavedWork() {
  return useSyncExternalStore(unsavedWork.subscribe, unsavedWork.getSnapshot, unsavedWork.getSnapshot);
}
/** Dirty entries outlive their view. Only save, explicit discard or a clean
 * baseline unregisters them; unmount is not consent to throw away work. */
export function useUnsavedDraft(id: string, kind: DraftKind, label: string, dirty: boolean | undefined, value: unknown,
  discard: () => void, parent?: string) {
  const latest = useRef({ value, discard });
  latest.current = { value, discard };
  const scope = unsavedWork.getScope();
  const scopeKey = JSON.stringify(scope);
  const ownerScope = useRef(scopeKey);
  const signature = JSON.stringify(value, (_key, item) => typeof File !== 'undefined' && item instanceof File
    ? { file: item.name, size: item.size, modified: item.lastModified, type: item.type } : item);
  useEffect(() => {
    if (dirty === undefined) return;
    if (ownerScope.current !== scopeKey || scopeKey !== JSON.stringify(unsavedWork.getScope())) return;
    if (dirty) unsavedWork.register({ id, kind, label, value: latest.current.value, parent,
      discard: () => latest.current.discard() });
    else unsavedWork.unregister(id);
  }, [id, kind, label, dirty, signature, scopeKey, parent]);
  return () => unsavedWork.unregister(id);
}
export function DraftSaveNote({ id }: { id: string }) {
  const entries = useUnsavedWork();
  const failed = entries.find(entry => entry.id === id && entry.persistence === 'failed');
  if (!failed) return null;
  return <p role="status" className="text-xs text-wait">{failed.problem === 'remove' ? "Couldn't remove the local draft. An older copy may return after reload; keep this tab open and retry." : "Couldn't save this draft locally. Keep this tab open or copy your changes before leaving."}
    {' '}<button type="button" className="underline focus-visible:outline-2 focus-visible:outline-accent" onClick={() => unsavedWork.flush(id)}>Retry local save</button></p>;
}
