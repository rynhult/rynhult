import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RelayInputGeneration } from '../src/lib/sessionRelayInput.ts';
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const writes = []; let claims = 0; let processed = 0; let idle = true;
  const transport = {
    async claim() { claims++; return { inputGrant: 'opaque', inputEpoch: 2 }; },
    async write(grant, sequence, data) { writes.push({ grant, sequence, data }); return { partial: false, watermark: { epoch: 2, admitted: sequence, processed } }; },
    async drain() { return { epoch: 2, admitted: writes.length, processed }; }, async idle() { return idle; },
  };
  return { transport, writes, generation: new RelayInputGeneration(2, transport, () => {}, async () => {}), claims: () => claims,
    process(value) { processed = value; }, busy() { idle = false; } };
}
test('reads never connect; explicit person connection happens once', async () => {
  const c = fixture(); await c.generation.refresh(); assert.equal(c.claims(), 0);
  await c.generation.connect(); await c.generation.connect(); assert.equal(c.claims(), 1);
});
test('a late claim cannot activate a retired generation', async () => {
  const c = fixture(); const held = deferred(); c.transport.claim = () => held.promise;
  const pending = c.generation.connect(); c.generation.retire(); held.resolve({ inputGrant: 'late', inputEpoch: 2 }); await pending;
  assert.equal(c.generation.state.phase, 'retired'); assert.equal(c.generation.enqueue('a'), false); assert.equal(c.writes.length, 0);
});
test('retirement retains buffered text and never forwards the remaining queue', async () => {
  const c = fixture(); const held = deferred(); c.transport.write = async (grant, sequence, data) => { c.writes.push({ grant, sequence, data }); return held.promise; };
  await c.generation.connect(); c.generation.enqueue('a'); await tick(); c.generation.enqueue('b'); c.generation.enqueue('c'); c.generation.retire();
  held.resolve({ partial: false, watermark: { epoch: 2, admitted: 1, processed: 0 } }); await tick();
  assert.equal(c.generation.state.draft, 'abc'); assert.equal(c.generation.state.phase, 'retired'); assert.equal(c.writes.length, 1);
});
test('transport acknowledgement cannot clear a draft or unlock later typing', async () => {
  const c = fixture(); await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('\r'); await tick(); await c.generation.refresh();
  assert.equal(c.generation.state.draft, 'a'); assert.equal(c.generation.state.waiting, true); assert.equal(c.generation.enqueue('b'), false);
  c.busy(); c.process(2); await c.generation.refresh(); assert.equal(c.generation.state.draft, ''); assert.equal(c.generation.state.waiting, true);
});
test('confirmed processing and a fresh idle read permit independent later typing', async () => {
  const c = fixture(); await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('\r'); await tick(); c.process(2); await c.generation.refresh();
  assert.equal(c.generation.state.waiting, false); assert.equal(c.generation.enqueue('b'), true); await tick(); assert.deepEqual(c.writes.map(w => w.sequence), [1, 2, 3]);
});
test('unknown writes freeze without retrying queued input', async () => {
  const c = fixture(); c.transport.write = async (...args) => { c.writes.push(args); throw new Error('lost reply'); };
  await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('b'); await tick(); await c.generation.refresh();
  assert.equal(c.generation.state.phase, 'uncertain'); assert.equal(c.generation.state.draft, 'ab'); assert.equal(c.writes.length, 1);
  await c.generation.connect(); assert.equal(c.claims(), 1);
});
for (const invalid of ['/', '!', '?', '\x1b', 'é', 'ab', '\n']) test(`unsupported input ${JSON.stringify(invalid)} has no outbound effect`, async () => {
  const c = fixture(); await c.generation.connect(); assert.throws(() => c.generation.enqueue(invalid)); assert.equal(c.writes.length, 0);
});

test('retirement during the native Enter quiet boundary never sends delayed Enter', async () => {
  const c = fixture(); const held = deferred();
  const generation = new RelayInputGeneration(2, c.transport, () => {}, () => held.promise);
  await generation.connect(); generation.enqueue('a'); generation.enqueue('\r'); await tick();
  generation.retire(); held.resolve(); await tick();
  assert.deepEqual(c.writes.map(frame => frame.data), ['a']); assert.equal(generation.state.draft, 'a');
});

test('an acknowledgement for another epoch freezes rather than advancing input', async () => {
  const c = fixture(); c.transport.write = async () => ({ partial: false, watermark: { epoch: 3, admitted: 1, processed: 0 } });
  await c.generation.connect(); c.generation.enqueue('a'); await tick();
  assert.equal(c.generation.state.phase, 'uncertain'); assert.equal(c.generation.state.draft, 'a');
  assert.equal(c.generation.enqueue('b'), false); assert.equal(c.claims(), 1);
});
test('a late idle read cannot unlock a retired input', async () => {
  const c = fixture(); const held = deferred(); c.transport.idle = () => held.promise;
  await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('\r'); await tick(); c.process(2);
  const pending = c.generation.refresh(); await tick(); c.generation.retire(); held.resolve(true); await pending;
  assert.equal(c.generation.state.phase, 'retired'); assert.equal(c.generation.enqueue('b'), false);
});
test('impossible processing beyond admitted input remains unknown with the draft retained', async () => {
  const c = fixture(); await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('\r'); await tick(); c.process(3);
  await c.generation.refresh(); assert.equal(c.generation.state.phase, 'uncertain'); assert.equal(c.generation.state.draft, 'a');
});


test('one animation frame coalesces keys, preserves edits and keeps Enter separate', async () => {
  const c = fixture(); const frame = deferred();
  const generation = new RelayInputGeneration(2, c.transport, () => {}, async () => {}, () => frame.promise);
  await generation.connect();
  for (const key of ['a', 'b', '\x7f', 'c', '\r']) generation.enqueue(key);
  assert.equal(generation.state.draft, 'ac'); assert.equal(c.writes.length, 0);
  frame.resolve(); await tick();
  assert.deepEqual(c.writes.map(w => [w.sequence, w.data]), [[1, 'ab\x7fc'], [2, '\r']]);
  c.process(2); await generation.refresh(); assert.equal(generation.state.waiting, false);
});
test('retirement while awaiting animation frame sends no buffered keys', async () => {
  const c = fixture(); const frame = deferred();
  const generation = new RelayInputGeneration(2, c.transport, () => {}, async () => {}, () => frame.promise);
  await generation.connect(); generation.enqueue('a'); generation.enqueue('b'); generation.retire();
  frame.resolve(); await tick(); assert.equal(c.writes.length, 0); assert.equal(generation.state.draft, 'ab');
});
test('partial batched write freezes all following input without splitting or replay', async () => {
  const c = fixture(); c.transport.write = async (grant, sequence, data) => {
    c.writes.push({ sequence, data }); return { partial: true, watermark: { epoch: 2, admitted: sequence, processed: 0 } };
  };
  await c.generation.connect(); c.generation.enqueue('a'); c.generation.enqueue('b'); c.generation.enqueue('\r'); await tick();
  assert.equal(c.generation.state.phase, 'uncertain'); assert.equal(c.generation.state.draft, 'ab');
  assert.deepEqual(c.writes, [{ sequence: 1, data: 'ab' }]);
});

test('an immediate first frame cannot strand a typing burst behind a tiny HTTP frame', async () => {
  const c = fixture(); const frames = [];
  const generation = new RelayInputGeneration(2, c.transport, () => {}, async () => {}, () => {
    const frame = deferred(); frames.push(frame); return frame.promise;
  });
  await generation.connect(); generation.enqueue('a');
  frames[0].resolve(); await tick(); assert.equal(c.writes.length, 0);
  generation.enqueue('b'); frames[1].resolve(); await tick(); assert.equal(c.writes.length, 0);
  generation.enqueue('c'); frames[2].resolve(); await tick();
  assert.deepEqual(c.writes.map(w => w.data), ['abc']); assert.equal(frames.length, 3);
});
