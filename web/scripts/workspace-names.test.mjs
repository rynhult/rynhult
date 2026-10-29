import { test } from 'node:test';
import assert from 'node:assert/strict';
import { workspaceName } from '../src/lib/format.ts';

for (const [folder, name] of [
  ['beyblade-tournament-apps-0-12345678-20261026', 'Beyblade tournament apps 0 12345678'],
  ['beyblade-tournament-apps-0-12345678-20261026-2', 'Beyblade tournament apps 0 12345678'],
  ['research-20250101-20261026', 'Research 20250101'],
  ['research-20250101-20261026-12', 'Research 20250101'],
  ['beyblade-tournament-apps-0-abcdef01-20261026', 'Beyblade tournament apps 0 abcdef01'],
  ['plain-topic', 'Plain topic'],
]) {
  test(`workspace title preserves the topic in ${folder}`, () => {
    assert.equal(workspaceName(folder), name);
  });
}
