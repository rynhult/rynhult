import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { patchXtermComposition, xtermCompositionFix } from './xterm-composition.mjs';

test('the locked installed xterm module accepts the reviewed compatibility fixes', () => {
  const path = import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs');
  const source = readFileSync(new URL(path), 'utf8');
  const old = 'this._textarea.value.substring(this._compositionPosition.start,this._compositionPosition.end)';
  const updated = patchXtermComposition(source);
  assert.notEqual(updated, source);
  assert.equal(updated.includes(old), false);
});

test('unknown, already-fixed and ambiguous upstream modules fail the build', () => {
  assert.throws(() => patchXtermComposition('changed upstream'), /exactly once/);
  const source = readFileSync(new URL(import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs')), 'utf8');
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
  const source = readFileSync(new URL(import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs')), 'utf8');
  fix.transform(source, '/node_modules/@xterm/xterm/lib/xterm.mjs');
  assert.doesNotThrow(() => fix.generateBundle());
  fix.buildStart();
  assert.throws(() => fix.generateBundle(), /was not applied/);
});

// A Process key can schedule fallback work before compositionstart. Under a busy
// event loop the whole composition may finish before that old callback runs.
test('a deferred Process-key fallback cannot replay a completed composition', async () => {
  const { runInNewContext } = await import('node:vm');
  const path = import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs');
  const source = patchXtermComposition(readFileSync(new URL(path), 'utf8'));
  const method = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  const queued = [], sent = [];
  const Subject = runInNewContext(`(class {
    ${method('compositionstart(){', 'compositionupdate(')}
    ${method('_handleAnyTextareaChanges(){', 'updateCompositionElements(')}
  })`, { setTimeout: fn => queued.push(fn) });
  const helper = new Subject();
  helper._textarea = { value: 'review ' };
  helper._compositionView = { textContent: '', classList: { add() {} } };
  helper._compositionPosition = {};
  helper._coreService = { triggerDataEvent: text => sent.push(text) };
  helper._handleAnyTextareaChanges();
  helper.compositionstart();
  helper._textarea.value += 'é';
  helper._isComposing = false;
  // The composition finalizer already sent the committed text.
  helper._coreService.triggerDataEvent('é');
  queued.shift()();
  assert.deepEqual(sent, ['é']);
  // A Process key with no intervening composition must still deliver native edits.
  helper._handleAnyTextareaChanges();
  helper._textarea.value += '日本';
  queued.shift()();
  assert.deepEqual(sent, ['é', '日本']);
});

for (const immediate of [true, false]) test(`a previous composition timer cannot consume the next commit (first sent immediately: ${immediate})`, async () => {
  const { runInNewContext } = await import('node:vm');
  const source = patchXtermComposition(readFileSync(new URL(import.meta.resolve('@xterm/xterm').replace('/lib/xterm.js', '/lib/xterm.mjs')), 'utf8'));
  const method = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  const queued = [], sent = [];
  const Subject = runInNewContext(`(class {
    ${method('compositionstart(){', 'compositionupdate(')}
    ${method('_finalizeComposition(t){', '_handleAnyTextareaChanges(')}
  })`, { setTimeout: fn => queued.push(fn) });
  const helper = new Subject();
  helper._textarea = { value: 'review ' };
  helper._compositionView = { textContent: '', classList: { add() {}, remove() {} } };
  helper._compositionPosition = {};
  helper._coreService = { triggerDataEvent: text => sent.push(text) };
  helper.compositionstart();
  helper._textarea.value += 'é';
  helper._finalizeComposition(true);
  if (immediate) helper._finalizeComposition(false);
  helper.compositionstart();
  helper._textarea.value += '日本';
  helper._finalizeComposition(true);
  while (queued.length) queued.shift()();
  assert.deepEqual(sent, ['é', '日本']);
});
