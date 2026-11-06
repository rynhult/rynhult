import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeChatItems, cleanChatText, safeChatLink, chatEvents } from '../src/lib/chat.ts';
const item = (n, text = String(n), revision = 'a') => ({ id: String(n).padStart(64, '0'), revision: revision.repeat(64), role: 'assistant', text });

test('upserts use identity, retain repeated text, and older pages cannot overwrite live revisions', () => {
  let result = mergeChatItems([item(2, 'same'), item(3, 'same')], [item(3, 'changed', 'b'), item(4, 'same')], 'follow');
  assert.deepEqual(result.items.map(x => x.text), ['same', 'changed', 'same']);
  result = mergeChatItems(result.items, [item(1, 'first'), item(3, 'stale')], 'older');
  assert.deepEqual(result.items.map(x => x.text), ['first', 'same', 'changed', 'same']);
  assert.equal(result.items[2].revision, 'b'.repeat(64));
});

test('bounds refuse growth without evicting the reader’s focused history', () => {
  const original = [item(1)];
  const result = mergeChatItems(original, Array.from({ length: 1001 }, (_, i) => item(i + 2)), 'older');
  assert.equal(result.overflow, true); assert.deepEqual(result.items, original);
  assert.equal(mergeChatItems([], [item(2, 'a'.repeat(2_100_000))], 'follow').overflow, true);
});

test('hostile terminal controls and bidi text cannot disguise content', () => {
  assert.equal(cleanChatText('\x1b[31mred\x1b[0m\u202esafe\x00\nline\tend'), 'redsafe\nline\tend');
  assert.equal(cleanChatText('\x1b]8;;https://evil.example.com\x07label\x1b]8;;\x07'), 'label');
});

for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'file:///etc/passwd', '//example.com', '/api/host', 'https://user:password@example.com', 'https://example.com/\nsecret']) test(`rejects unsafe link ${JSON.stringify(url)}`, () => assert.equal(safeChatLink(url), null));
test('only explicit HTTP links without credentials are clickable', () => assert.equal(safeChatLink('https://example.com/a?b=1'), 'https://example.com/a?b=1'));

const envelope = { session_id: 's', run_id: 1, state: 'ready', historical_run_attribution: 'unknown', observed_at: null,
  items: [item(1, '日本')], omitted: 0, reset: false, cursor: 'opaque', before_cursor: null,
  capabilities: { read_history: true, follow_messages: true, rich_tools: false } };
function response(chunks) { return new Response(new ReadableStream({ start(controller) { for (const chunk of chunks) controller.enqueue(chunk); controller.close(); } }), { headers: { 'Content-Type': 'text/event-stream' } }); }
test('SSE reads split UTF-8 and CRLF without appending partial or duplicate messages', async () => {
  const bytes = new TextEncoder().encode(`event: chat\r\ndata: ${JSON.stringify(envelope)}\r\n\r\n`);
  const rows = [];
  for await (const row of chatEvents(response(Array.from(bytes, byte => Uint8Array.of(byte))))) rows.push(row);
  assert.deepEqual(rows, [envelope]);
});
test('unfinished SSE records are not committed and oversized events refuse', async () => {
  const rows = [];
  for await (const row of chatEvents(response([new TextEncoder().encode(`event: chat\ndata: ${JSON.stringify(envelope)}`)]))) rows.push(row);
  assert.deepEqual(rows, []);
  await assert.rejects(async () => { for await (const _row of chatEvents(response([new TextEncoder().encode('x'.repeat(1_048_577))]))) { /* consume */ } }, /large/);
});

test('heartbeat upserts keep the existing item array so idle following does not reparse history', () => {
  const current = [item(1)];
  assert.equal(mergeChatItems(current, [], 'follow').items, current);
  assert.equal(mergeChatItems(current, [{ ...current[0] }], 'follow').items, current);
  assert.equal(mergeChatItems(current, [{ ...current[0] }], 'older').items, current);
});

test('bounded verification and incomplete records can continue without claiming an empty history', async () => {
  const { canFollow } = await import('../src/lib/chat.ts');
  const unknown = { ...envelope, state: 'unknown', items: [], capabilities: { read_history: false, follow_messages: false, rich_tools: false } };
  assert.equal(canFollow(unknown), false);
  assert.equal(canFollow({ ...unknown, more: true, reason: 'prefix_verification' }), true);
  assert.equal(canFollow({ ...unknown, reason: 'partial_record' }), true);
  assert.equal(canFollow({ ...unknown, more: true, reason: 'discovery_budget' }), false);
  assert.equal(canFollow({ ...unknown, state: 'unsupported', more: true }), false);
});

test('unreadable metadata and unqualified tool/private roles fail closed', async () => {
  const { chatPage } = await import('../src/lib/chat.ts');
  for (const bad of [null, { ...envelope, state: 'made-up' }, { ...envelope, omitted: -1 },
    { ...envelope, items: [{ ...item(1), role: 'system' }] }, { ...envelope, items: [{ ...item(1), role: 'tool', arguments: 'private' }] },
    { ...envelope, notices: { tools: -1 } }, { ...envelope, more: 'yes' }]) assert.throws(() => chatPage(bad), /unreadable/);
});

test('control stripping is linear on repeated unterminated OSC sequences', () => {
  const input = 'start' + '\x1b]'.repeat(65536);
  const started = performance.now();
  assert.equal(cleanChatText(input), 'start');
  assert.ok(performance.now() - started < 200, 'bounded linear scan, not a repeated suffix search');
});

test('error, offline and busy frames never advance the last consumed cursor', async () => {
  const { acceptChatPage } = await import('../src/lib/chat.ts');
  const previous = { ...envelope, cursor: 'good', state: 'gap', omitted: 5 };
  for (const state of ['error', 'offline', 'busy']) {
    const accepted = acceptChatPage(previous, { ...envelope, state, cursor: 'POISON' });
    assert.equal(accepted.cursor, 'good');
    assert.equal(accepted.omitted, 5);
  }
});
test('a heartbeat or metadata recheck cannot erase known gaps; only a replaced snapshot can', async () => {
  const { acceptChatPage } = await import('../src/lib/chat.ts');
  const gap = { ...envelope, state: 'gap', omitted: 5 };
  const heartbeat = acceptChatPage(gap, { ...envelope, state: 'ready', omitted: 0 });
  assert.equal(heartbeat.incomplete, true); assert.equal(heartbeat.omitted, 5);
  assert.equal(acceptChatPage(heartbeat, { ...envelope }, true).incomplete, false);
});
