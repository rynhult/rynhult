import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { patchXtermComposition, xtermCompositionFix } from './xterm-composition.mjs';

test('the locked installed xterm module gets exactly the reviewed one-line fix', () => {
  const path = import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs');
  const source = readFileSync(new URL(path), 'utf8');
  const old = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
  const updated = patchXtermComposition(source);
  assert.equal(updated.replace('this._textarea.value.substring(this._compositionPosition.start)', old), source);
});

test('unknown, already-fixed and ambiguous upstream modules fail the build', () => {
  assert.throws(() => patchXtermComposition('changed upstream'), /exactly once/);
  const source = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
  assert.throws(() => patchXtermComposition(source + source), /exactly once/);
  assert.throws(() => patchXtermComposition(patchXtermComposition(source)), /exactly once/);
});

test('the transform only touches the xterm module, including query and Windows paths', () => {
  const fix = xtermCompositionFix();
  assert.equal(fix.transform('unchanged', '/web/src/components/Terminal.tsx'), undefined);
  assert.throws(() => fix.transform('changed', 'C:\\web\\node_modules\\@xterm\\xterm\\lib\\xterm.mjs?v=123'), /exactly once/);
});


test('a changed entry point cannot silently skip the fix in a production build', () => {
  const fix = xtermCompositionFix();
  fix.buildStart();
  assert.throws(() => fix.generateBundle(), /was not applied/);
  const source = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
  fix.transform(source, '/node_modules/@xterm/xterm/lib/xterm.mjs');
  assert.doesNotThrow(() => fix.generateBundle());
  fix.buildStart();
  assert.throws(() => fix.generateBundle(), /was not applied/);
});
