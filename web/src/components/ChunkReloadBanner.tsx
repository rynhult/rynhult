import { useEffect, useRef, useSyncExternalStore } from "react";
import { getChunkFailure, subscribeToChunkFailure } from "../lib/lazyImport";
import { useInstance } from "../lib/queries";
import { useConfirm } from "./confirm";
import { announce, Button } from "./ui";

const loadFailureMessage = "Rynhult was updated, reload to continue";

export function ChunkReloadBanner() {
  const failure = useSyncExternalStore(subscribeToChunkFailure, getChunkFailure);
  const confirm = useConfirm();
  const instance = useInstance();
  const build = 'build' in instance ? instance.build : undefined;
  const loadedBuild = useRef(build);
  if (!loadedBuild.current && build) loadedBuild.current = build;
  const changed = !!build && !!loadedBuild.current && build !== loadedBuild.current;
  const message = changed ? 'A new version is available — reload' : loadFailureMessage;
  useEffect(() => { if (failure || changed) announce(message); }, [failure, changed, message]);
  if (!failure && !changed) return null;
  return <section aria-label="Update required" className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-wait/30 bg-panel px-4 py-3 text-[13px]">
    <div className="min-w-0">
      <p className="font-medium text-wait">{message}</p>
      <p className="text-xs text-muted">{changed ? 'Save or copy unsaved work before reloading to use the updated app.' : 'A part of the app could not be downloaded. Check your connection, and save or copy unsaved work before reloading.'}</p>
    </div>
    <Button onClick={async () => {
      if (await confirm({ title: "Reload Rynhult?", body: "Save or copy unsaved input first. Reloading discards edits and unsent text that have not been saved.", confirm: "Reload" })) window.location.reload();
    }}>Reload</Button>
  </section>;
}
