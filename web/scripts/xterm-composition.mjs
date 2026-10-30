/**
 * Compatibility fix for @xterm/xterm 6.0.0 (MIT; its notice stays in THIRD_PARTY_NOTICES.md).
 * CompositionHelper._finalizeComposition(false) can run from keydown before the
 * compositionupdate timer updates .end. substring(start, staleEnd) swaps reversed
 * endpoints and sends already typed text instead of the committed composition.
 * Read through the current textarea end, as the delayed finalizer already does.
 * Also invalidate a deferred Process-key fallback if composition has since
 * started: it must not replay a composition that completed before its timer ran.
 * Bind delayed finalizers to that generation too, flushing a pending commit
 * before a new composition starts so consecutive commits are not lost.
 *
 * Patch the build's in-memory module, never the installed package or machine.
 * Fail closed if the target expression or entry changes; review this fix on xterm updates.
 */
// The 229 fallback must be bound to the composition generation it observed.
// Merely checking _isComposing when its timer runs misses an entire completed
// composition and resends text that the finalizer already delivered.
const startBefore = 'compositionstart(){this._isComposing=!0';
const startAfter = 'compositionstart(){this._isSendingComposition&&this._finalizeComposition(!1),this._accCompositionEpoch=(this._accCompositionEpoch??0)+1,this._isComposing=!0';
const fallbackBefore = '_handleAnyTextareaChanges(){let t=this._textarea.value;setTimeout(()=>{if(!this._isComposing){';
const fallbackAfter = '_handleAnyTextareaChanges(){let t=this._textarea.value,epoch=this._accCompositionEpoch;setTimeout(()=>{if(!this._isComposing&&epoch===this._accCompositionEpoch){';
const finalizeBefore = 'this._isSendingComposition=!0,setTimeout(()=>{if(this._isSendingComposition){';
const finalizeAfter = 'this._isSendingComposition=!0;const epoch=this._accCompositionEpoch;setTimeout(()=>{if(this._isSendingComposition&&epoch===this._accCompositionEpoch){';
const before = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
const after = 'this._textarea.value.substring(this._compositionPosition.start)';

export function patchXtermComposition(source) {
  if ([before, startBefore, fallbackBefore, finalizeBefore].some(target => source.split(target).length !== 2)) {
    throw new Error('xterm composition fix no longer matches exactly once; review it before updating xterm.');
  }
  return source.replace(before, after).replace(startBefore, startAfter).replace(fallbackBefore, fallbackAfter).replace(finalizeBefore, finalizeAfter);
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
