import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { selectRelayView, relayEnabled } from '../src/lib/sessionRelaySelector.ts';
import { operationDigest } from '../src/lib/sessionRelayDigest.ts';
const fixtures = name => JSON.parse(readFileSync(new URL(`../../tests/fixtures/session_relay/${name}.json`, import.meta.url))).cases;
for (const c of fixtures('selector')) test(c.name, () => assert.deepEqual(selectRelayView(c.state), c.expect));
for (const c of fixtures('digests')) test(`digest: ${c.name}`, async () => assert.equal(await operationDigest(c.method, c.params), c.digest));
test('both flag and immutable launch opt-in are required; omitted flag defaults off', () => {
  assert.equal(relayEnabled({}, true), false);
  assert.equal(relayEnabled({ session_relay: true }, false), false);
  assert.equal(relayEnabled({ session_relay: false }, true), false);
  assert.equal(relayEnabled({ session_relay: true }, true), true);
});
test('second client sees a pending switch rather than starting another', () => {
  const state = fixtures('selector')[2].state;
  assert.equal(selectRelayView(state).action, 'switch');
  assert.equal(selectRelayView({ ...state, switch_state: 'prepared' }).action, 'pending');
  assert.equal(selectRelayView({ ...state, owner: 'chat' }).action, 'view_only');
});
