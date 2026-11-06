import { useState, useSyncExternalStore, type SetStateAction } from 'react';
import { emptyDocument, type FileDocument } from './documentRegistry';
import { useWorkingView } from './workingView';
const noop = () => () => {};
/** Legacy Files keeps its route-local state; working Files has one registry-owned buffer/revision. */
export function useFileDocument<K extends keyof FileDocument>(field: K): [FileDocument[K], (value: SetStateAction<FileDocument[K]>) => void] {
  const working = useWorkingView();
  const [local, setLocal] = useState<FileDocument[K]>(emptyDocument[field]);
  const snapshot = useSyncExternalStore(working?.documents.subscribe ?? noop, () => working ? working.documents.get(working.key) : emptyDocument);
  return working ? [snapshot[field], value => {
    const old = working.documents.get(working.key)[field];
    working.documents.update(working.key, { [field]: typeof value === 'function' ? (value as (old: FileDocument[K]) => FileDocument[K])(old) : value });
  }] : [local, setLocal];
}
