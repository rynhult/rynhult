import { test } from 'node:test';
import assert from 'node:assert/strict';
import { themeCss, previewHtml } from '../src/lib/community-theme.ts';
const theme = { palettes: { light: { bg: '#ffffff', text: '#10191f' }, dark: { bg: '#10191f', text: '#ffffff' } },
  selection: { light: { background: '#414fa8', foreground: '#ffffff' }, dark: { background: '#abb6ff', foreground: '#000000' } }, presets: { light: {}, dark: {} } };
test('generated CSS has only mapped variables, opaque selection and no terminal contribution', () => {
  const css = themeCss(theme);
  assert.match(css, /--bg:#ffffff/); assert.match(css, /::selection/);
  assert.doesNotMatch(css, /term-bg|url\(|@import/);
});
test('untrusted server or cache token values fail closed', () => {
  for (const tokens of [{ bg: 'red;display:none' }, { 'term-bg': '#ffffff' }, { 'x}body{': '#ffffff' }]) {
    assert.throws(() => themeCss({ ...theme, palettes: { ...theme.palettes, dark: tokens } }));
  }
  assert.throws(() => themeCss({ ...theme, presets: { ...theme.presets, dark: { sans: 'url(evil)' } } }));
});
test('preview is static synthetic markup with a network-blocking CSP', () => {
  const html = previewHtml(theme, 'light');
  assert.match(html, /default-src 'none'/); assert.match(html, /Terminal palette stays built in/);
  assert.doesNotMatch(html, /<script|<form|<a |https?:|<img/);
  assert.match(html, /Chat/); assert.match(html, /Inbox/); assert.match(html, /Files/);
});

test('canonical package digest golden vector agrees across Python and JavaScript', async () => {
  const { readFileSync } = await import('node:fs');
  const { createHash } = await import('node:crypto');
  const vector = JSON.parse(readFileSync(new URL('../../tests/fixtures/theme-digest-v1.json', import.meta.url), 'utf8'));
  const sorted = value => value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0).map(([k,v]) => [k,sorted(v)])) : value;
  const hash = createHash('sha256').update('rynhult-theme-v1\0');
  const parts = [Buffer.from(JSON.stringify(sorted(vector.manifest)))];
  for (const [name, data] of Object.entries(vector.files).sort()) parts.push(Buffer.from(name),Buffer.from(data));
  for (const part of parts) { const size = Buffer.alloc(8); size.writeBigUInt64BE(BigInt(part.length)); hash.update(size).update(part); }
  assert.equal(`sha256:${hash.digest('hex')}`, vector.digest);
});

test('omitted font presets still emit bundled stacks with system fallbacks', () => {
  const css = themeCss(theme);
  assert.match(css, /--font-sans:"Inter Variable",system-ui,sans-serif/);
  assert.match(css, /--font-mono:"JetBrains Mono Variable",monospace/);
});
