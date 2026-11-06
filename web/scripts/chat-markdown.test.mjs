import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseChatMarkdown } from '../src/lib/chat-markdown.ts';
test('hostile emphasis and deep nesting select plain text without recursive parsing', () => {
  for (const text of ['*a'.repeat(60000), '~a'.repeat(60000), '> '.repeat(8000), '> '.repeat(40) + 'deep', '[a]('.repeat(30000)]) {
    const start = performance.now();
    assert.equal(parseChatMarkdown(text).plain, true);
    assert.ok(performance.now() - start < 100);
  }
});
test('short safe links and code use bounded nonrecursive tokens', () => {
  const parsed = parseChatMarkdown('[safe](https://example.com/help)\n```text\nconst value = 1;\n```');
  assert.equal(parsed.plain, false);
  assert.ok(parsed.blocks.some(block => block.kind === 'code'));
  assert.equal(parsed.blocks[0].spans[0].url, 'https://example.com/help');
});
test('depth, size, line and token budgets fall back without discarding the original text', () => {
  for (const text of ['a'.repeat(4097), Array(130).fill('a').join('\n'), '**a** '.repeat(200)]) {
    const parsed = parseChatMarkdown(text);
    assert.equal(parsed.plain, true); assert.equal(parsed.text, text);
  }
});

test('UI-10 ordinary commands and arithmetic retain literal delimiters', () => {
  for (const text of ['rm *.log *.tmp', '2 * 3 * 4', 'glob src/*/test/*.py',
    'grep -r ** src ** done', 'x **** y']) {
    const parsed = parseChatMarkdown(text);
    const shown = parsed.plain ? parsed.text : parsed.blocks.map(block => block.kind === 'code' ? block.text : block.spans.map(span => span.text).join('')).join('\n');
    assert.equal(shown, text);
  }
});
test('UI-10 inline code consumes matching delimiter runs and keeps inner backticks', () => {
  const parsed = parseChatMarkdown('use ``a`b`` here');
  assert.equal(parsed.plain, false);
  assert.equal(parsed.blocks[0].spans.map(span => span.text).join(''), 'use a`b here');
  assert.deepEqual(parsed.blocks[0].spans.filter(span => span.kind === 'code'), [{kind: 'code', text: 'a`b'}]);
});
test('flat emphasis requires nonspace content and word boundaries', () => {
  const parsed = parseChatMarkdown('*em* and **strong**');
  assert.deepEqual(parsed.blocks[0].spans.filter(span => span.kind !== 'text'), [{kind: 'em', text: 'em'}, {kind: 'strong', text: 'strong'}]);
  for (const text of ['word*inside*word', '* bad*', '*bad *', '** **']) {
    assert.equal(parseChatMarkdown(text).text, text);
    assert.equal(parseChatMarkdown(text).plain, true);
  }
});

const shown = parsed => parsed.plain ? parsed.text : parsed.blocks.map(block => block.kind === 'code' ? block.text : block.spans.map(span => span.text).join('')).join('\n');
test('UI-15 code-like brackets and refused link targets stay literal', () => {
  for (const text of ['handlers[key](event)', 'arr[0](x) ok', 'see [docs](javascript:alert(1)) now', '[docs](./guide.md) and more',
    'vec![a](b)', '![local](./a.png)', 'x[y](https://example.com/z)']) {
    const parsed = parseChatMarkdown(text);
    assert.equal(shown(parsed), text);
    assert.equal(parsed.blocks.flatMap(block => block.spans ?? []).some(span => span.url), false);
  }
});
test('UI-15 safe links and the remote image placeholder keep their existing form', () => {
  const link = parseChatMarkdown('see [docs](https://example.com/help) now');
  assert.deepEqual(link.blocks[0].spans, [{kind: 'text', text: 'see '}, {kind: 'link', text: 'docs', url: 'https://example.com/help'}, {kind: 'text', text: ' now'}]);
  assert.equal(shown(parseChatMarkdown('![remote](https://example.com/tracker.png)')), '[Image omitted: remote]');
});
test('UI-15 refused targets are consumed once and cannot start emphasis inside them', () => {
  const parsed = parseChatMarkdown('[a](*b*) and *c*');
  assert.equal(shown(parsed), '[a](*b*) and c');
  assert.deepEqual(parsed.blocks[0].spans.filter(span => span.kind !== 'text'), [{kind: 'em', text: 'c'}]);
  const hostile = '[a](b) x[a](b '.repeat(280), start = performance.now();
  assert.equal(shown(parseChatMarkdown(hostile)), hostile);
  assert.ok(performance.now() - start < 100);
});
test('UI-16 fences close only on a bare fence and text beside a fence is kept', () => {
  const trailing = parseChatMarkdown('```js\nx*y\n``` tail\nafter');
  assert.deepEqual(trailing.blocks, [{kind: 'code', text: 'x*y\n``` tail\nafter'}]);
  const closed = parseChatMarkdown('```\ncode\n```  \nafter');
  assert.deepEqual(closed.blocks.map(block => block.kind), ['code', 'text']);
  assert.equal(shown(closed), 'code\nafter');
  assert.equal(shown(parseChatMarkdown('``` tail\nmore')), '``` tail\nmore');
  const inline = parseChatMarkdown('```inline``` text');
  assert.deepEqual(inline.blocks[0].spans, [{kind: 'code', text: 'inline'}, {kind: 'text', text: ' text'}]);
});
