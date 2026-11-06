import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DocumentRegistry } from '../src/lib/documentRegistry.ts';
test('one canonical document retains buffer and base revision across view aliases', () => {
  const registry = new DocumentRegistry();
  registry.open('file-a');
  registry.update('file-a', { opened: { content: 'disk', revision: 'r1' }, draft: 'unsaved' });
  assert.equal(registry.bind('file-a', 'placement', 'checkout-1'), 'file-a');
  registry.open('file-b');
  assert.equal(registry.bind('file-b', 'placement', 'checkout-1'), 'file-a');
  assert.equal(registry.get('file-a').draft, 'unsaved');
  assert.equal(registry.get('file-a').opened.revision, 'r1');
  assert.throws(() => registry.bind('file-a', 'placement', 'checkout-2'), /changed/);
  registry.close('file-b'); registry.close('file-a');
  assert.equal(registry.size, 0);
});
test('document ownership has an explicit bounded capacity; rejected updates retain previous text', () => {
  const registry = new DocumentRegistry();
  for (let i = 0; i < 8; i++) registry.open(String(i));
  assert.throws(() => registry.open('ninth'), /limit/);
  registry.update('0', { draft: 'kept' });
  assert.throws(() => registry.update('0', { draft: 'x'.repeat(1024 * 1024 + 1) }), /limit/);
  assert.equal(registry.get('0').draft, 'kept');
  registry.clear(); assert.equal(registry.size, 0);
});
