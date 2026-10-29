/**
 * Compatibility fix for @xterm/xterm 6.0.0 (MIT; its notice stays in THIRD_PARTY_NOTICES.md).
 * CompositionHelper._finalizeComposition(false) can run from keydown before the
 * compositionupdate timer updates .end. substring(start, staleEnd) swaps reversed
 * endpoints and sends already typed text instead of the committed composition.
 * Read through the current textarea end, as the delayed finalizer already does.
 *
 * Patch the build's in-memory module, never the installed package or machine.
 * Fail closed if the target expression or entry changes; review this fix on xterm updates.
 */
const before = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
const after = 'this._textarea.value.substring(this._compositionPosition.start)';

export function patchXtermComposition(source) {
  if (source.split(before).length !== 2) {
    throw new Error('xterm composition fix no longer matches exactly once; review it before updating xterm.');
  }
  return source.replace(before, after);
}

export function xtermCompositionFix() {
  let applied = false;
  return {
    name: 'xterm-composition-boundary',
    enforce: 'pre',
    buildStart() { applied = false; },
    generateBundle() {
      if (!applied) throw new Error('xterm composition fix was not applied; review the resolved xterm entry point.');
    },
    transform(source, id) {
      if (!id.replaceAll('\\', '/').split('?')[0].endsWith('/node_modules/@xterm/xterm/lib/xterm.mjs')) return;
      const code = patchXtermComposition(source);
      applied = true;
      return { code, map: null };
    },
  };
}
