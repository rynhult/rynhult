import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ViewRegistry, terminalKey } from '../src/lib/viewRegistry.ts';

const ref = (sessionId = 's', runId = 'run-1') => ({ installation: 'instance', person: 'owner', sessionId, runId, node: 'node', account: 'agent' });
function fixture(options = {}) {
  let clock = 0;
  const handles = [];
  const policies = [];
  const registry = new ViewRegistry({ ...options, now: () => clock, create(reference, policy) {
    const handle = { reference, writerToken: null, composing: false, state: 'open', disposed: 0,
      visible: false, slot: null, place(slot) { this.slot = slot; },
      setVisibility(value) { this.visible = value; }, dispose() { this.disposed++; } };
    handles.push(handle); policies.push(policy); return handle;
  } });
  return { registry, handles, policies, tick: ms => { clock += ms; } };
}

test('same scoped run shares one handle across views and 20 placements', () => {
  const { registry, handles, policies } = fixture();
  registry.registerSlot('a', { label: 'a' }); registry.registerSlot('b', { label: 'b' });
  const first = registry.acquire(ref()), second = registry.acquire(ref());
  assert.notEqual(first.viewId, second.viewId);
  assert.equal(handles.length, 1); assert.deepEqual(policies, ['p2-observer']);
  for (let i = 0; i < 20; i++) registry.place(i % 2 ? first.viewId : second.viewId, i % 2 ? 'a' : 'b');
  assert.equal(handles.length, 1); assert.equal(handles[0].disposed, 0);
  registry.closeView(first.viewId); registry.closeView(second.viewId);
  assert.equal(handles[0].visible, false); assert.equal(handles[0].disposed, 0);
  assert.equal(registry.inspect(first.resourceKey).views, 0);
});

test('identity includes installation, person, node, account and process incarnation', () => {
  const { registry, handles } = fixture();
  registry.acquire(ref());
  for (const field of ['installation', 'person', 'sessionId', 'runId', 'node', 'account']) registry.acquire({ ...ref(), [field]: 'other' });
  assert.equal(handles.length, 7);
  assert.throws(() => registry.acquire({ ...ref(), runId: '' }), /identity/);
  const copy = ref(); const key = terminalKey(copy); registry.acquire(copy); copy.sessionId = 'changed';
  assert.equal(registry.inspect(key).reference.sessionId, 's');
});

test('P1 claim exception is explicit and never the default', () => {
  const { registry, policies } = fixture({ writerPolicy: 'p1-claim-if-free' });
  registry.acquire(ref()); assert.deepEqual(policies, ['p1-claim-if-free']);
});

test('visible budget refuses safely while attachment pressure evicts a hidden view', () => {
  const { registry, handles } = fixture();
  for (let i = 0; i < 8; i++) registry.registerSlot(`slot-${i}`, {});
  const views = Array.from({ length: 8 }, (_, i) => registry.acquire(ref(String(i))));
  for (let i = 0; i < 4; i++) registry.place(views[i].viewId, `slot-${i}`);
  assert.throws(() => registry.place(views[4].viewId, 'slot-4'), /visible.*limit/);
  assert.equal(handles[4].visible, false);
  registry.acquire(ref('ninth')); assert.equal(handles.length, 9);
  registry.setVisibility(views[0].viewId, false); registry.place(views[4].viewId, 'slot-4');
  assert.equal(handles[0].visible, false); assert.equal(handles.at(-1).visible, true);
});

for (const pin of ['writer', 'composing', 'dirty', 'unsent', 'uncertain']) test(`${pin} pins a hidden terminal against eviction`, () => {
  const { registry, handles, tick } = fixture({ maxAttached: 1 });
  const view = registry.acquire(ref());
  if (pin === 'writer') handles[0].writerToken = {};
  else if (pin === 'composing') handles[0].composing = true;
  else registry.setPins(view.resourceKey, { [pin]: true });
  tick(600_001); registry.sweep();
  assert.equal(handles[0].disposed, 0);
  registry.acquire(ref('other')); assert.equal(handles[0].disposed, 0);
  assert.equal(handles.length, 2);
});

test('only hidden unpinned entries expire after ten minutes, with a detached placeholder', () => {
  const { registry, handles, tick } = fixture();
  registry.registerSlot('a', {});
  const visible = registry.acquire(ref('visible')), hidden = registry.acquire(ref('hidden'));
  registry.place(visible.viewId, 'a');
  tick(599_999); registry.sweep(); assert.equal(handles[1].disposed, 0);
  tick(2); registry.sweep();
  assert.equal(handles[0].disposed, 0); assert.equal(handles[1].disposed, 1);
  assert.equal(registry.inspect(hidden.resourceKey).state, 'detached');
  assert.throws(() => registry.place(hidden.viewId, 'a'), /occupied/);
  const fresh = registry.acquire(ref('hidden'));
  assert.equal(handles.length, 3); assert.equal(fresh.resourceKey, hidden.resourceKey);
});

test('unregistering a slot hides its host without disconnecting or claiming', () => {
  const { registry, handles } = fixture();
  const remove = registry.registerSlot('a', {}); const view = registry.acquire(ref()); registry.place(view.viewId, 'a');
  remove(); remove();
  assert.equal(handles[0].visible, false); assert.equal(handles[0].disposed, 0);
  assert.throws(() => registry.place(view.viewId, 'a'), /slot/);
});

test('closing a secondary view never hides the canonical view', () => {
  const { registry, handles } = fixture(); registry.registerSlot('a', {});
  const first = registry.acquire(ref()), second = registry.acquire(ref()); registry.place(first.viewId, 'a');
  registry.setVisibility(second.viewId, false); registry.closeView(second.viewId);
  assert.equal(handles[0].visible, true);
});

test('explicit release refuses unsafe input/drafts, revocation disposes exactly once and clears metadata', () => {
  const { registry, handles } = fixture(); const view = registry.acquire(ref());
  registry.setPins(view.resourceKey, { dirty: true });
  assert.throws(() => registry.dispose(view.resourceKey, 'release'), /unsaved|pending/);
  registry.dispose(view.resourceKey, 'permission-revoked'); registry.dispose(view.resourceKey, 'permission-revoked');
  assert.equal(handles[0].disposed, 1); assert.equal(registry.inspect(view.resourceKey), undefined);
  assert.throws(() => registry.place(view.viewId, 'a'), /view/);
});

test('factory failure leaves a retryable detached view and shutdown disposes everything once', () => {
  const { registry, handles } = fixture();
  registry.acquire(ref()); registry.acquire(ref('second')); registry.shutdown(); registry.shutdown();
  assert.deepEqual(handles.map(x => x.disposed), [1, 1]);
  assert.throws(() => registry.acquire(ref('after')), /shutdown/);
  const failed = new ViewRegistry({ create() { throw new Error('private path or token'); } });
  const view = failed.acquire(ref());
  assert.equal(failed.inspect(view.resourceKey).state, 'detached');
  assert.equal(failed.inspect(view.resourceKey).reason, 'Terminal attachment unavailable.');
});

test('slot conflicts and failed show requests preserve the old canonical host', () => {
  const { registry, handles } = fixture(); registry.registerSlot('a', {}); registry.registerSlot('b', {});
  const first = registry.acquire(ref()), second = registry.acquire(ref('second'));
  registry.place(first.viewId, 'a'); registry.place(second.viewId, 'b');
  assert.throws(() => registry.place(first.viewId, 'b'), /occupied/);
  assert.equal(handles[0].visible, true); assert.equal(handles[1].visible, true);
  assert.equal(registry.inspect(first.resourceKey).canonicalView, first.viewId);
  registry.setVisibility(first.viewId, false); registry.place(second.viewId, 'a');
  assert.throws(() => registry.setVisibility(first.viewId, true), /occupied/);
  assert.equal(handles[0].visible, false);
});

test('a visible resource starts a fresh idle interval only when hidden', () => {
  const { registry, handles, tick } = fixture(); registry.registerSlot('a', {});
  const view = registry.acquire(ref()); registry.place(view.viewId, 'a');
  tick(900_000); registry.sweep(); assert.equal(handles[0].disposed, 0);
  registry.setVisibility(view.viewId, false); tick(599_999); registry.sweep(); assert.equal(handles[0].disposed, 0);
  tick(2); registry.sweep(); assert.equal(handles[0].disposed, 1);
});

test('budget pressure evicts only eligible hidden entries; safe pin clearing allows eviction', () => {
  const { registry, handles, tick } = fixture({ maxAttached: 1 });
  const first = registry.acquire(ref()); registry.setPins(first.resourceKey, { dirty: true });
  tick(100); registry.acquire(ref('other')); assert.equal(handles[0].disposed, 0);
  registry.setPins(first.resourceKey, { dirty: false });
  registry.acquire(ref('other')); assert.equal(handles[0].disposed, 1); assert.equal(handles.length, 2);
  const snapshot = registry.inspect(first.resourceKey); snapshot.pins.dirty = true; snapshot.reference.person = 'changed';
  assert.equal(registry.inspect(first.resourceKey).pins.dirty, false);
  assert.equal(registry.inspect(first.resourceKey).reference.person, 'owner');
});

test('invalid budgets, unknown views and duplicate slots are refused', () => {
  assert.throws(() => fixture({ maxAttached: Infinity }), /budget/);
  assert.throws(() => fixture({ writerPolicy: 'take' }), /policy/);
  const { registry } = fixture(); registry.registerSlot('a', {});
  assert.throws(() => registry.registerSlot('a', {}), /already/);
  assert.throws(() => registry.setVisibility('missing', true), /view/);
  assert.throws(() => registry.setPins('missing', { dirty: true }), /resource/);
});

test('idle sweeps dispose eligible resources in oldest-hidden order', () => {
  const { registry, handles, tick } = fixture(); const order = [];
  registry.acquire(ref('oldest')); tick(1000); registry.acquire(ref('newer'));
  for (const handle of handles) { const dispose = handle.dispose.bind(handle); handle.dispose = () => { order.push(handle.reference.sessionId); dispose(); }; }
  tick(600_001); registry.sweep();
  assert.deepEqual(order, ['oldest', 'newer']);
});

test('idle eviction drops unreferenced metadata rather than accumulating closed sessions', () => {
  const { registry, tick } = fixture(); const view = registry.acquire(ref());
  registry.closeView(view.viewId); tick(600_001); registry.sweep();
  assert.equal(registry.inspect(view.resourceKey), undefined);
});

test('abandoning uncertain terminal input is explicit and cannot discard composition or a dirty document', () => {
  const { registry, handles } = fixture(); const view = registry.acquire(ref());
  registry.setPins(view.resourceKey, { uncertain: true });
  assert.throws(() => registry.dispose(view.resourceKey, 'release'), /pending input/);
  handles[0].composing = true;
  assert.throws(() => registry.dispose(view.resourceKey, 'discard-input'), /pending input/);
  handles[0].composing = false;
  registry.dispose(view.resourceKey, 'discard-input');
  assert.equal(handles[0].disposed, 1);
});

test('all pinned allows one overflow, defers the next and evicts oldest when unpinned', () => {
  const { registry, handles, tick } = fixture({ maxAttached: 2 });
  const oldest = registry.acquire(ref('oldest')); registry.setPins(oldest.resourceKey, { unsent: true });
  tick(1);
  const newer = registry.acquire(ref('newer')); registry.setPins(newer.resourceKey, { dirty: true });
  const overflow = registry.acquire(ref('overflow')); registry.setPins(overflow.resourceKey, { uncertain: true });
  const queued = registry.acquire(ref('queued'));
  assert.equal(handles.length, 3); assert.equal(registry.inspect(queued.resourceKey).reason, undefined);
  registry.setPins(oldest.resourceKey, { unsent: false });
  assert.equal(handles[0].disposed, 1); assert.equal(handles[1].disposed, 0);
  registry.setPins(newer.resourceKey, { dirty: false });
  registry.acquire(ref('next'));
  assert.equal(handles.filter(h => !h.disposed).length, 2);
});

for (const kind of ['visible', 'composing']) test(`overflow evicts marked ${kind} entry after it unpins`, () => {
  const { registry, handles } = fixture({ maxAttached: 1 });
  registry.registerSlot('a', {});
  const first = registry.acquire(ref());
  if (kind === 'visible') registry.place(first.viewId, 'a');
  else handles[0].composing = true;
  registry.acquire(ref('overflow'));
  assert.equal(handles[0].disposed, 0);
  if (kind === 'visible') registry.setVisibility(first.viewId, false);
  else { handles[0].composing = false; registry.sweep(); }
  assert.equal(handles[0].disposed, 1);
});

test('a queued view reconnects on sweep when capacity returns and keeps its requested slot', () => {
  const { registry, handles } = fixture({ maxAttached: 1 });
  registry.registerSlot('a', {});
  const first = registry.acquire(ref()); registry.setPins(first.resourceKey, { unsent: true });
  const extra = registry.acquire(ref('extra')); registry.setPins(extra.resourceKey, { unsent: true });
  const queued = registry.acquire(ref('queued')); registry.place(queued.viewId, 'a');
  assert.equal(registry.inspect(queued.resourceKey).waiting, true);
  assert.equal(handles.length, 2);
  registry.dispose(first.resourceKey, 'shutdown'); registry.dispose(extra.resourceKey, 'shutdown');
  registry.sweep();
  assert.equal(handles.length, 3); assert.equal(handles[2].visible, true);
  assert.equal(registry.inspect(queued.resourceKey).waiting, false);
  registry.acquire(ref('queued')); assert.equal(handles.length, 3);
});
