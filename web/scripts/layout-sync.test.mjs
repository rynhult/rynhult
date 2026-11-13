import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LayoutSync } from '../src/lib/layoutSync.ts';
import { fileTarget, fromServer, keepViews, resolvePlaceholders, toServer } from '../src/lib/workingSerial.ts';

const scope = { installation: 'instance', person: 'person', project_id: 1, device: 'desktop', viewport: 'wide' };
const record = (revision, layout = null, resources = {}) => ({ scope, revision, schema_version: 2, layout, resources });
const one = id => ({ schema_version: 2, views: [{ id, kind: 'page' }], columns: [{ tabs: [id], active: id, width: 1 }], focused: 0, filled: null });
class Memory {
  data = new Map();
  get length() { return this.data.size; }
  key(i) { return [...this.data.keys()][i] ?? null; }
  getItem(k) { return this.data.get(k) ?? null; }
  setItem(k, v) { this.data.set(k, v); }
  removeItem(k) { this.data.delete(k); }
}
class Server {
  current = record(0); online = true; writes = [];
  transport = async (_, init) => {
    if (!this.online) throw new TypeError('offline');
    if (!init) return structuredClone(this.current);
    this.writes.push(init);
    if (init.json.expected_revision !== this.current.revision) throw Object.assign(new Error('conflict'), { status: 409, data: { current: structuredClone(this.current) } });
    this.current = record(this.current.revision + 1, init.json.layout ?? null);
    return structuredClone(this.current);
  };
}
const sync = (server, storage = new Memory()) => {
  const seen = []; const item = new LayoutSync('/layouts', { transport: server.transport, storage, delay: 0, retry: 5, onChange: view => seen.push(view.status) });
  return { item, seen, storage };
};
const settled = (item, status) => new Promise((resolve, reject) => {
  const started = Date.now();
  const check = () => item.view.status === status ? resolve() : Date.now() - started > 1000 ? reject(new Error(`still ${item.view.status}, wanted ${status}`)) : setTimeout(check, 2);
  check();
});

test('loading never writes, and an edit is saved by compare-and-swap', async () => {
  const server = new Server(); server.current = record(4, one('saved'));
  const { item } = sync(server);
  assert.equal((await item.start()).record.layout.views[0].id, 'saved');
  assert.equal(server.writes.length, 0);
  item.change(one('edited')); await settled(item, 'synced');
  assert.equal(server.writes[0].json.expected_revision, 4);
  assert.equal(server.current.revision, 5);
  item.close();
});

test('a stale window never overwrites: this window wins only when chosen, by a new compare-and-swap', async () => {
  const server = new Server(); const a = sync(server), b = sync(server);
  await a.item.start(); await b.item.start();
  a.item.change(one('a')); await settled(a.item, 'synced');
  b.item.change(one('b')); await settled(b.item, 'conflict');
  assert.equal(server.current.layout.views[0].id, 'a');
  b.item.chooseThisWindow(); await settled(b.item, 'synced');
  assert.equal(server.current.layout.views[0].id, 'b');
  assert.equal(server.writes.at(-1).json.expected_revision, 1);
  a.item.close(); b.item.close();
});

test('loading the newer layout discards only the pending arrangement and writes nothing', async () => {
  const server = new Server(); const a = sync(server), b = sync(server);
  await a.item.start(); await b.item.start();
  a.item.change(one('a')); await settled(a.item, 'synced');
  b.item.change(one('b')); await settled(b.item, 'conflict');
  const writes = server.writes.length;
  assert.equal(b.item.chooseNewer().layout.views[0].id, 'a');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(server.writes.length, writes);
  assert.equal(b.storage.data.size, 0);
  a.item.close(); b.item.close();
});

test('a conflict keeps later edits waiting for the choice', async () => {
  const server = new Server(); const a = sync(server), b = sync(server);
  await a.item.start(); await b.item.start();
  a.item.change(one('a')); await settled(a.item, 'synced');
  b.item.change(one('b')); await settled(b.item, 'conflict');
  const writes = server.writes.length;
  b.item.change(one('later')); await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(server.writes.length, writes); assert.equal(b.item.view.status, 'conflict');
  b.item.chooseThisWindow(); await settled(b.item, 'synced');
  assert.equal(server.current.layout.views[0].id, 'later');
  a.item.close(); b.item.close();
});

test('offline saves honestly, survives a reload, and is sent on return', async () => {
  const server = new Server(); const first = sync(server);
  await first.item.start();
  server.online = false;
  first.item.change(one('offline')); await settled(first.item, 'offline');
  assert.equal(first.item.view.durable, true);
  first.item.close();
  server.online = true;
  const second = sync(server, first.storage);
  const started = await second.item.start();
  assert.equal(started.pending.views[0].id, 'offline');
  await settled(second.item, 'synced');
  assert.equal(server.current.layout.views[0].id, 'offline');
  assert.equal(second.storage.data.size, 0);
  second.item.close();
});

test('offline retries by itself when the server returns', async () => {
  const server = new Server(); const { item } = sync(server);
  await item.start(); server.online = false;
  item.change(one('later')); await settled(item, 'offline');
  server.online = true; await settled(item, 'synced');
  assert.equal(server.current.layout.views[0].id, 'later');
  item.close();
});

test('an offline copy older than the server is a conflict, not a send', async () => {
  const server = new Server(); const first = sync(server);
  await first.item.start(); server.online = false;
  first.item.change(one('offline')); await settled(first.item, 'offline');
  first.item.close(); server.online = true; server.current = record(7, one('elsewhere'));
  const second = sync(server, first.storage);
  await second.item.start();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(second.item.view.status, 'conflict'); assert.equal(server.writes.length, 0);
  second.item.close();
});

test('a reset is a compare-and-swap too', async () => {
  const server = new Server(); server.current = record(2, one('old'));
  const { item } = sync(server);
  await item.start(); item.change(null); await settled(item, 'synced');
  assert.equal(server.current.layout, null); assert.equal(server.writes[0].json.expected_revision, 2);
  item.close();
});

test('edits made before the server was reachable never replace a layout this window has not seen', async () => {
  const server = new Server(); server.online = false; server.current = record(3, one('unseen'));
  const { item } = sync(server);
  await item.start(); assert.equal(item.view.status, 'local');
  item.change(one('mine'));
  server.online = true; await settled(item, 'conflict');
  assert.equal(server.writes.length, 0);
  item.chooseThisWindow(); await settled(item, 'synced');
  assert.equal(server.current.layout.views[0].id, 'mine');
  assert.equal(server.current.revision, 4);
  item.close();
});

test('a project without a server layout stays local and says so', async () => {
  const server = { transport: async () => { throw Object.assign(new Error('missing'), { status: 404 }); } };
  const { item } = sync(server);
  assert.equal((await item.start()).record, null);
  assert.equal(item.view.status, 'local'); assert.match(item.view.note, /this visit/);
  item.close();
});

test('F1: looking at the newer layout settles nothing, so a refused placement stays a conflict', async () => {
  const server = new Server(); const a = sync(server), b = sync(server);
  await a.item.start(); await b.item.start();
  a.item.change(one('a')); await settled(a.item, 'synced');
  b.item.change(one('b')); await settled(b.item, 'conflict');
  const writes = server.writes.length;
  assert.equal(b.item.newer().layout.views[0].id, 'a');
  assert.equal(b.item.view.status, 'conflict');
  b.item.change(one('later')); await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(server.writes.length, writes); assert.equal(server.current.layout.views[0].id, 'a');
  a.item.close(); b.item.close();
});

test('F2: a change that matches what the server holds writes nothing', async () => {
  const server = new Server(); server.current = record(4, one('saved'));
  const { item } = sync(server);
  await item.start();
  item.change({ ...one('saved'), columns: [{ width: 1, active: 'saved', tabs: ['saved'] }] });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(server.writes.length, 0);
  item.change(one('edited')); await settled(item, 'synced');
  item.change(one('edited')); await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(server.writes.length, 1);
  item.close();
});

test('F7: a save whose reply was lost is recognised as this window\'s own, not another window\'s', async () => {
  const server = new Server(); const { item } = sync(server);
  await item.start();
  const transport = server.transport; let lose = true;
  server.transport = async (path, init) => { const reply = await transport(path, init); if (init && lose) { lose = false; throw new TypeError('reply lost'); } return reply; };
  const lossy = new LayoutSync('/layouts', { transport: (...args) => server.transport(...args), storage: new Memory(), delay: 0, retry: 5, onChange: () => {} });
  await lossy.start();
  lossy.change(one('mine')); await settled(lossy, 'offline'); await settled(lossy, 'synced');
  assert.equal(server.current.revision, 1); assert.equal(server.current.layout.views[0].id, 'mine');
  lossy.close(); item.close();
});

const view = (key, href, kind) => ({ key, href, kind, minWidth: 320 });
const working = views => ({ version: 1, views: Object.fromEntries(views.map(v => [v.key, v])), focused: 0, filled: null,
  columns: [{ tabs: views.map(v => v.key), active: views[0].key, width: 1 }] });
const file = view('file:project:1:docs/secret plan.md', '/projects/1?tab=files&file=docs%2Fsecret+plan.md', 'file');

test('a saved layout names files only by reference, and restores them through the resolved location', () => {
  const refs = new Map([[file.key, 'opaque-reference']]);
  const { layout, omitted } = toServer(working([file, view('session:abc:terminal', '/sessions/abc', 'terminal')]), refs);
  assert.equal(omitted, 0);
  assert.ok(!JSON.stringify(layout).includes('secret'));
  assert.deepEqual(layout.views[0], { id: 'f-opaque-reference', kind: 'file', file_ref: 'opaque-reference' });
  const back = fromServer(record(1, layout, { 'f-opaque-reference': { available: true, file: { location: 'project', id: '1', folder: '', path: 'docs/secret plan.md' } }, 't-abc': { available: true } }));
  assert.deepEqual(Object.keys(back.layout.views), [file.key, 'session:abc:terminal']);
  assert.equal(back.refs.get(file.key), 'opaque-reference');
});

test('a file view with no reference yet is left out and counted, never saved by path', () => {
  const { layout, omitted } = toServer(working([file, view('session:abc:terminal', '/sessions/abc', 'terminal')]), new Map());
  assert.equal(omitted, 1);
  assert.deepEqual(layout.views.map(v => v.id), ['t-abc']);
  assert.ok(!JSON.stringify(layout).includes('secret'));
});

test('unknown, unauthorized and unnamed references restore as placeholders and survive the next save', () => {
  const layout = { schema_version: 2, focused: 0, filled: null, columns: [{ tabs: ['f-gone', 't-lost', 'bare'], active: 'f-gone', width: 1 }], views: [
    { id: 'f-gone', kind: 'file', file_ref: 'gone' }, { id: 't-lost', kind: 'terminal', resource: { kind: 'session', id: 'lost' } }, { id: 'bare', kind: 'page' }] };
  const back = fromServer(record(1, layout, { 'f-gone': { available: false }, 't-lost': { available: false }, bare: { available: false } })).layout;
  for (const item of Object.values(back.views)) { assert.equal(item.kind, 'unavailable'); assert.equal(item.href, ''); }
  assert.deepEqual(toServer(back, new Map()).layout, layout);
});

test('a forged resolved location cannot leave the known routes', () => {
  const layout = { schema_version: 2, focused: 0, filled: null, columns: [{ tabs: ['f-x'], active: 'f-x', width: 1 }], views: [{ id: 'f-x', kind: 'file', file_ref: 'x' }] };
  const back = fromServer(record(1, layout, { 'f-x': { available: true, file: { location: 'project', id: '1', folder: '', path: '../../secret' } } })).layout;
  assert.equal(Object.values(back.views)[0].kind, 'unavailable');
});

test('page and chat views keep their tab, and hashes stay out of the saved layout', () => {
  const { layout } = toServer(working([view('page:/projects/1?tab=preview', '/projects/1?tab=preview', 'page'), view('session:abc:chat', '/sessions/abc?tab=chat', 'chat')]), new Map());
  assert.deepEqual(layout.views.map(v => [v.id, v.tab]), [['pp-1-preview', 'preview'], ['c-abc', undefined]]);
  const back = fromServer(record(1, layout, {})).layout;
  assert.deepEqual(Object.values(back.views).map(v => v.href), ['/projects/1?tab=preview', '/sessions/abc?tab=chat']);
});

test('taking another arrangement keeps views holding drafts or control', () => {
  const current = working([file, view('session:abc:terminal', '/sessions/abc', 'terminal')]);
  const incoming = working([view('session:other:terminal', '/sessions/other', 'terminal')]);
  assert.deepEqual(Object.keys(keepViews(incoming, current, [file.key]).views), ['session:other:terminal', file.key]);
  assert.deepEqual(Object.keys(keepViews(null, current, [file.key]).views), [file.key]);
});

test('a save reply only fills in placeholders: views opened since it was sent stay placed', () => {
  const layout = { schema_version: 2, focused: 0, filled: null, columns: [{ tabs: ['f-kept'], active: 'f-kept', width: 1 }], views: [{ id: 'f-kept', kind: 'file', file_ref: 'kept' }] };
  const restored = fromServer(record(1, null, {}), layout).layout;
  const key = Object.keys(restored.views)[0]; assert.equal(restored.views[key].kind, 'unavailable');
  const newer = view('session:abc:terminal', '/sessions/abc', 'terminal');
  const book = { ...restored, views: { ...restored.views, [newer.key]: newer }, columns: [{ tabs: [key, newer.key], active: newer.key, width: 1 }] };
  const resources = { 'f-kept': { available: true, file: { location: 'project', id: '1', folder: '', path: 'a.md' } } };
  const { layout: next, refs } = resolvePlaceholders(book, resources);
  assert.deepEqual(next.columns[0].tabs, ['file:project:1:a.md', newer.key]);
  assert.equal(next.columns[0].active, newer.key);
  assert.equal(refs.get('file:project:1:a.md'), 'kept');
  assert.equal(resolvePlaceholders(next, resources).layout, next); // nothing to do keeps the same object
});

test('F4: a tab outside the saved vocabulary is omitted, not sent to fail every later save', () => {
  const { layout, omitted } = toServer(working([view('page:/projects/1?tab=Weird', '/projects/1?tab=Weird', 'page'), view('session:abc:terminal', '/sessions/abc', 'terminal')]), new Map());
  assert.equal(omitted, 1); assert.deepEqual(layout.views.map(v => v.id), ['t-abc']);
});

test('a workspace file keeps its folder only in the resolved location', () => {
  assert.deepEqual(fileTarget(view('file:workspace:abc:a.md', '/workspaces/cedar/files?session=abc&file=a.md', 'file')), { location: 'workspace', id: 'abc', folder: 'cedar', path: 'a.md' });
  assert.equal(fileTarget(view('session:abc:terminal', '/sessions/abc', 'terminal')), null);
});

test('offline file placeholders become removable only after the server confirms loss', () => {
  const layout = { schema_version: 2, focused: 0, filled: null, columns: [{ tabs: ['f-lost'], active: 'f-lost', width: 1 }], views: [{ id: 'f-lost', kind: 'file', file_ref: 'lost' }] };
  const restored = fromServer(record(1, null, {}), layout).layout;
  const key = Object.keys(restored.views)[0];
  assert.equal(restored.views[key].unrestorable, false);
  assert.equal(resolvePlaceholders(restored, {}).layout, restored);
  const next = resolvePlaceholders(restored, { 'f-lost': { available: false } }).layout;
  assert.equal(next.views[key].unrestorable, true);
  assert.equal(resolvePlaceholders(next, { 'f-lost': { available: false } }).layout, next);
});
