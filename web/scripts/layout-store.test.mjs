import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PersonalLayoutStore } from '../src/lib/layoutStore.ts';
const scope = { installation: 'instance-a', person: 'alex', project_id: 1, device: 'desktop', viewport: 'wide' };
const placement = { columns: [{ tabs: ['main'], active: 'main', width: 1 }], focused: 0, filled: null };
const layout = { schema_version: 2, views: [{ id: 'main', kind: 'page' }], ...placement, draft: 'not saved' };
const one = id => ({ schema_version: 2, views: [{ id, kind: 'page' }], columns: [{ tabs: [id], active: id, width: 1 }], focused: 0, filled: null });
const normalized = { schema_version: 2, views: [{ id: 'main', kind: 'page' }], ...placement };
const record = (revision = 0, value = null, identity = scope) => ({ scope: identity, revision, schema_version: 2, layout: value, resources: {} });
class Memory {
  data = new Map();
  get length() { return this.data.size; }
  key(i) { return [...this.data.keys()][i] ?? null; }
  getItem(k) { return this.data.get(k) ?? null; }
  setItem(k, v) { this.data.set(k, v); }
  removeItem(k) { this.data.delete(k); }
}

test('offline pending copy survives reload, strips unknown contents, never auto-saves', async () => {
  const storage = new Memory();
  let online = true, writes = 0;
  const transport = async (_, init) => {
    if (!online) throw TypeError('offline');
    if (init) { writes++; return record(1, normalized); }
    return record();
  };
  const first = new PersonalLayoutStore(scope, transport, storage);
  await first.load();
  assert.equal(first.stage(layout).durable, true);
  online = false;
  assert.equal((await first.save()).status, 'offline');
  assert.ok(![...storage.data.values()][0].includes('not saved'));
  const second = new PersonalLayoutStore(scope, transport, storage);
  assert.deepEqual((await second.load()).pending.layout, normalized);
  online = true;
  await second.load();
  assert.equal(writes, 0);
  assert.equal((await second.save()).status, 'saved');
  assert.equal(storage.length, 0);
});

test('CAS conflict retains both copies, explicit local choice uses new revision', async () => {
  const storage = new Memory();
  let last;
  const store = new PersonalLayoutStore(scope, async (_, init) => {
    if (!init) return record(1);
    last = init;
    if (init.json.expected_revision === 1) throw { status: 409, data: { current: record(2, one('other')) } };
    return record(3, normalized);
  }, storage);
  await store.load(); store.stage(layout);
  assert.equal((await store.save()).status, 'conflict');
  assert.equal((await store.save()).status, 'conflict');
  store.resolve('local');
  assert.equal((await store.save()).status, 'saved');
  assert.equal(last.json.expected_revision, 2);
});

test('server choice discards pending only by explicit action; scope isolates people/devices/instances', async () => {
  const storage = new Memory();
  const first = new PersonalLayoutStore(scope, async () => record(), storage);
  await first.load(); first.stage(layout);
  for (const identity of [{ ...scope, person: 'blair' }, { ...scope, installation: 'b' }, { ...scope, viewport: 'narrow' }]) {
    const other = new PersonalLayoutStore(identity, async () => record(0, null, identity), storage);
    assert.equal((await other.load()).pending, null);
  }
  const conflicted = new PersonalLayoutStore(scope, async () => record(1), storage);
  assert.equal((await conflicted.load()).status, 'conflict');
  assert.equal(conflicted.resolve('server').pending, null);
  assert.equal(storage.length, 0);
});

test('quota/read failures are visible, authorization loss clears the copy', async () => {
  const broken = { getItem() { throw Error('blocked'); }, setItem() { throw Error('quota'); }, removeItem() { throw Error('blocked'); } };
  const store = new PersonalLayoutStore(scope, async () => record(), broken);
  assert.match((await store.load()).note, /unavailable/);
  assert.equal(store.stage(layout).durable, false);
  const storage = new Memory();
  let allowed = true;
  const revoked = new PersonalLayoutStore(scope, async () => { if (!allowed) throw { status: 403 }; return record(); }, storage);
  await revoked.load(); revoked.stage(layout); allowed = false;
  assert.equal((await revoked.load()).status, 'refused');
  assert.equal(storage.length, 0);
});

test('a later edit remains pending when an earlier save finishes; reset uses the audited route', async () => {
  let finish, lastPath;
  const store = new PersonalLayoutStore(scope, async (path, init) => {
    lastPath = path;
    if (!init) return record();
    return new Promise(resolve => { finish = resolve; });
  }, new Memory());
  await store.load(); store.stage(layout);
  const saving = store.save();
  store.stage(one('later'));
  finish(record(1, normalized));
  const saved = await saving;
  assert.equal(saved.status, 'pending');
  assert.equal(saved.pending.expected_revision, 1);
  assert.equal(saved.pending.layout.views[0].id, 'later');
  store.stage(null);
  const resetting = store.save();
  assert.ok(lastPath.endsWith('/reset'));
  finish(record(2));
  assert.equal((await resetting).status, 'saved');
});

test('dispose prevents an in-flight reply from repopulating personal state', async () => {
  let finish;
  const store = new PersonalLayoutStore(scope, async () => new Promise(resolve => { finish = resolve; }), new Memory());
  const loading = store.load(); store.dispose(); finish(record());
  await assert.rejects(loading, /disposed/);
  assert.throws(() => store.stage(layout), /disposed/);
});

test('another tab changing the retained copy is not overwritten or deleted', async () => {
  const storage = new Memory();
  const first = new PersonalLayoutStore(scope, async () => record(), storage);
  const second = new PersonalLayoutStore(scope, async () => record(), storage);
  await first.load(); await second.load();
  first.stage(layout);
  const raw = [...storage.data.values()][0];
  assert.equal(second.stage(one('second')).durable, false);
  second.dispose();
  assert.equal([...storage.data.values()][0], raw);
});

test('an unreadable retained copy is not replaced after a transient read failure', async () => {
  const storage = new Memory();
  const first = new PersonalLayoutStore(scope, async () => record(), storage);
  await first.load(); first.stage(layout);
  const raw = [...storage.data.values()][0];
  const get = storage.getItem.bind(storage);
  storage.getItem = () => { throw Error('blocked'); };
  const second = new PersonalLayoutStore(scope, async () => record(), storage);
  storage.getItem = get;
  await second.load();
  assert.equal(second.stage(one('empty')).durable, false);
  assert.equal([...storage.data.values()][0], raw);
});
