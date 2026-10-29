let failure: Error | null = null;
const listeners = new Set<() => void>();
const awaitingReload = new Promise<never>(() => {});

export const getChunkFailure = () => failure;
export function subscribeToChunkFailure(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function isLoadFailure(error: unknown): error is Error {
  return error instanceof Error && /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS for/i.test(error.message);
}

async function retryStylesheet(error: Error) {
  const prefix = "Unable to preload CSS for ";
  if (!error.message.startsWith(prefix)) return;
  const href = error.message.slice(prefix.length);
  const previous = Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]'))
    .find((link) => link.href === href);
  if (!previous) throw error;
  // Vite remembers the failed preload. Retry its stylesheet explicitly, before importing the module.
  const next = previous.cloneNode() as HTMLLinkElement;
  await new Promise<void>((resolve, reject) => {
    next.onload = () => resolve();
    next.onerror = () => reject(error);
    previous.replaceWith(next);
  });
}

/** Keep the literal import in the callback so Vite can split it and preload its dependencies. */
export async function lazyImport<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (error) {
    if (!isLoadFailure(error)) throw error;
    await new Promise<void>((resolve) => setTimeout(resolve, 500));
    try {
      await retryStylesheet(error);
      return await load();
    } catch (retryError) {
      if (!isLoadFailure(retryError)) throw retryError;
      console.error("App module failed to load after one retry", retryError);
      if (!failure) {
        failure = retryError;
        listeners.forEach((listener) => listener());
      }
      // Rejecting would unmount the nearest error boundary's tree, including unsaved editor/form state.
      // Leave only the unavailable lazy part suspended; the app-level banner offers an explicit reload.
      return awaitingReload;
    }
  }
}
