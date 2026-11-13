import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const cwd = fileURLToPath(new URL('../', import.meta.url));
function collect(native, grep) {
  const env = { ...process.env };
  delete env.ACC_E2E_RELAY_NATIVE;
  if (native) env.ACC_E2E_RELAY_NATIVE = '1';
  const args = ['node_modules/@playwright/test/cli.js', 'test', 'e2e/session-relay.spec.ts',
    '--project=desktop-stateful', '--list', '--reporter=json'];
  if (grep) args.push('--grep', '@relay-native');
  const result = spawnSync(process.execPath, args, { cwd, env, encoding: 'utf8', timeout: 30000 });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const specs = [];
  function visit(suite) {
    specs.push(...(suite.specs ?? []));
    for (const child of suite.suites ?? []) visit(child);
  }
  for (const suite of JSON.parse(result.stdout).suites) visit(suite);
  return specs.filter(spec => !spec.file.endsWith('00-setup.spec.ts'));
}

test('default browser collection excludes native labs; explicit opt-in preserves every case', () => {
  const portable = collect(false, false);
  const all = collect(true, false);
  const native = collect(true, true);
  const key = spec => `${spec.file}:${spec.line}:${spec.title}`;
  assert.ok(portable.length > 0);
  assert.ok(native.length > 0);
  assert.ok(portable.every(spec => !spec.tags.includes('relay-native')));
  assert.ok(native.every(spec => spec.tags.includes('relay-native')));
  assert.deepEqual(new Set([...portable, ...native].map(key)), new Set(all.map(key)));
  assert.equal(portable.length + native.length, all.length);
  for (const title of ['native retained owner 1:', 'offline native:', 'offline native Claude:',
    'persisted native final screen', 'RC G7.R1 G2.R1:']) {
    assert.ok(native.some(spec => spec.title.startsWith(title)), `Missing native lab family: ${title}`);
  }
});
