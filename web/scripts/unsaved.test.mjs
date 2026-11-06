import assert from 'node:assert/strict';
import { test } from 'node:test';
import { UnsavedWorkRegistry, unsavedWork } from '../src/lib/unsaved.ts';
import { saveDraft, loadDraft, clearDraft } from '../src/lib/drafts.ts';

function browser(t) {
  const data = new Map(), listeners = new Map();
  for (const [key, value] of Object.entries({ sessionStorage: {
    getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k),
  }, window: { location: { origin: 'https://example.lan' }, addEventListener: (k, f) => listeners.set(k, f),
    removeEventListener: k => listeners.delete(k), dispatchEvent: () => {} } })) {
    const old = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, value });
    t.after(() => old ? Object.defineProperty(globalThis, key, old) : delete globalThis[key]);
  }
  unsavedWork.setScope({ installation: 'https://example.lan', person: 'alex' });
  t.after(() => unsavedWork.setScope(null));
  return { data, listeners };
}

test('one guard covers retained participants, explicit discard and flush', t => {
  const { listeners } = browser(t);
  const registry = new UnsavedWorkRegistry();
  registry.setScope({ installation: 'https://example.lan', person: 'alex' });
  let discarded = 0, flushed = 0;
  registry.register({ id: 'one', kind: 'files', label: 'File edits', value: 'private content',
    discard: () => discarded++, flush: () => { flushed++; return false; } });
  registry.register({ id: 'two', kind: 'plan', label: 'Plan', value: 'private plan', discard: () => {} });
  assert.equal(listeners.size, 1);
  assert.equal(registry.getSnapshot().length, 2);
  assert(!JSON.stringify(registry.getSnapshot()).includes('private'));
  assert.equal(registry.flush('one'), false); assert.equal(flushed, 1);
  const e = new Event('beforeunload', { cancelable: true }); listeners.get('beforeunload')(e);
  assert.equal(e.defaultPrevented, true);
  registry.discard('one'); assert.equal(discarded, 1);
  registry.unregister('two'); assert.equal(listeners.size, 0);
});

test('person and installation boundaries clear retained values and callbacks', t => {
  browser(t);
  unsavedWork.register({ id: 'brief:1', kind: 'brief', label: 'Project brief', value: 'private', discard: () => {} });
  unsavedWork.setScope({ installation: 'https://example.lan', person: 'blair' });
  assert.equal(unsavedWork.read('brief:1'), undefined);
  assert.deepEqual(unsavedWork.getSnapshot(), []);
  unsavedWork.setScope(null);
  assert.equal(unsavedWork.register({ id: 'x', kind: 'brief', label: 'Brief', discard: () => {} }), false);
});

for (const problem of ['SecurityError', 'QuotaExceededError']) test(`storage ${problem} reports failure and retains current edit`, t => {
  browser(t);
  assert.equal(saveDraft('plan:1', { idea: 'older' }), true);
  sessionStorage.setItem = () => { throw new DOMException('Blocked', problem); };
  assert.equal(saveDraft('plan:1', { idea: 'newer' }), false);
  assert.deepEqual(loadDraft('plan:1'), { idea: 'newer' });
  assert.equal(unsavedWork.getSnapshot()[0].persistence, 'failed');
  clearDraft('plan:1'); assert.deepEqual(unsavedWork.getSnapshot(), []);
});

test('scoped persistence never imports another person or unscoped legacy keys', t => {
  const { data } = browser(t);
  data.set('acc-draft:brief:1', JSON.stringify('legacy private'));
  assert.equal(loadDraft('brief:1'), null);
  assert.equal(saveDraft('brief:1', 'alex only'), true);
  unsavedWork.setScope({ installation: 'https://example.lan', person: 'blair' });
  assert.equal(loadDraft('brief:1'), null);
  unsavedWork.setScope({ installation: 'https://example.lan', person: 'alex' });
  assert.equal(loadDraft('brief:1'), 'alex only');
});

test('persistence discard acts on the editor and retry acknowledges only the current value', t => {
  const { data } = browser(t);
  let discarded = false;
  sessionStorage.setItem = () => { throw new Error('blocked'); };
  assert.equal(saveDraft('plan:1', { idea: 'current' }, () => { discarded = true; }), false);
  sessionStorage.setItem = (key, value) => data.set(key, value);
  assert.equal(unsavedWork.flush('plan:1'), true);
  assert.equal(unsavedWork.getSnapshot()[0].persistence, 'local');
  assert.deepEqual(loadDraft('plan:1'), { idea: 'current' });
  unsavedWork.discard('plan:1');
  assert.equal(discarded, true);
  assert.equal(loadDraft('plan:1'), null);
});

test('an authority change cannot restore the owner copy into a member editor', t => {
  browser(t);
  const scope = { installation: 'https://example.lan', person: 'alex' };
  unsavedWork.setScope({ ...scope, authority: 'owner' });
  saveDraft('brief:1', 'owner draft');
  unsavedWork.setScope({ ...scope, authority: 'member' });
  assert.equal(loadDraft('brief:1'), null);
  assert.deepEqual(unsavedWork.getSnapshot(), []);
});

test('a stale discard confirmation cannot discard a newer edit', t => {
  browser(t);
  let discarded = 0;
  unsavedWork.register({ id: 'plan:1', kind: 'plan', label: 'Plan', value: 'first', discard: () => discarded++ });
  const version = unsavedWork.getSnapshot()[0].version;
  unsavedWork.register({ id: 'plan:1', kind: 'plan', label: 'Plan', value: 'newer', discard: () => discarded++ });
  assert.equal(unsavedWork.discard('plan:1', version), false);
  assert.equal(discarded, 0);
  assert.equal(unsavedWork.read('plan:1'), 'newer');
});

test('DR-6 a write without a scope remains volatile and reports failure', t => {
  browser(t); unsavedWork.setScope(null);
  assert.equal(saveDraft('plan:unknown', { idea: 'keep volatile edit' }), false);
  assert.equal(unsavedWork.getSnapshot()[0]?.persistence, 'failed');
  assert.deepEqual(loadDraft('plan:unknown'), { idea: 'keep volatile edit' });
  unsavedWork.setScope({ installation: 'https://example.lan', person: 'blair' });
  assert.equal(loadDraft('plan:unknown'), null);
});

test('DR-8 refused removal retains a tombstone, warning and retry', t => {
  const { data } = browser(t); saveDraft('plan:1', 'older copy');
  sessionStorage.removeItem = () => { throw new Error('blocked'); };
  assert.equal(clearDraft('plan:1'), false);
  assert.equal(loadDraft('plan:1'), null);
  assert.equal(unsavedWork.getSnapshot()[0].problem, 'remove');
  sessionStorage.removeItem = key => data.delete(key);
  assert.equal(unsavedWork.flush('plan:1'), true);
  assert.equal(loadDraft('plan:1'), null);
  assert.deepEqual(unsavedWork.getSnapshot(), []);
});

test('DR-15 a clean empty slot stays clean when removal is blocked', t => {
  browser(t);
  sessionStorage.removeItem = () => { throw new Error('blocked'); };
  assert.equal(clearDraft('brief:clean-empty'), true);
  assert.deepEqual(unsavedWork.getSnapshot(), []);
});

test('DR-15 a known saved copy retains its tombstone when storage becomes unreadable', t => {
  browser(t); saveDraft('brief:known-saved', 'saved before storage failed');
  sessionStorage.getItem = sessionStorage.removeItem = () => { throw new Error('blocked'); };
  assert.equal(clearDraft('brief:known-saved'), false);
  assert.equal(unsavedWork.getSnapshot()[0].problem, 'remove');
});

test('DR-15 unreadable storage with no known copy does not invent a dirty draft', t => {
  browser(t);
  sessionStorage.getItem = sessionStorage.removeItem = () => { throw new Error('blocked'); };
  assert.equal(clearDraft('brief:never-saved'), true);
  assert.deepEqual(unsavedWork.getSnapshot(), []);
});
