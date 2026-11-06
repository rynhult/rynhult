import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TerminalInputState } from '../src/lib/terminalInputState.ts';

test('arrows, mouse reports, automatic replies and control keys do not create a partial prompt', () => {
  const input = new TerminalInputState();
  for (const bytes of ['\x1b[A', '\x1b[<0;12;14M', '\x1b[M !!', '\x1b[1;1R', '\x1b]10;rgb:ffff/ffff/ffff\x07', '\t', '\x7f', '\x1b[', 'D']) {
    input.push(bytes); assert.equal(input.partial, false);
  }
});
test('printable Unicode and paste stay partial until an explicit line or editing boundary', () => {
  const input = new TerminalInputState();
  input.push('日本'); assert.equal(input.partial, true);
  input.push('\x1b[A'); assert.equal(input.partial, true);
  input.push('\x15'); assert.equal(input.partial, false);
  input.push('\x1b[200~pasted\x1b[201~'); assert.equal(input.partial, true);
  input.push('\r'); assert.equal(input.partial, false);
  input.push('answer\nnext'); assert.equal(input.partial, true);
  input.push('\x03'); assert.equal(input.partial, false);
});
