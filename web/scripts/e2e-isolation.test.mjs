import './relay-native-policy.test.mjs';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { assertSafeWorkers, assertCleanBoundary, baseTestImports } from '../e2e/isolation-policy.ts';

const projects = [
  { name: 'setup', dependencies: [] },
  { name: 'desktop-stateful', workers: 1, dependencies: ['setup'] },
  { name: 'mobile-stateful', workers: 1, dependencies: ['setup'] },
  { name: 'desktop', dependencies: ['setup'] },
  { name: 'mobile', dependencies: ['setup'] },
  { name: 'terminal-chromium', workers: 1, dependencies: ['mobile-stateful'] },
];
const config = (names, workers) => ({ projects: [...projects], filteredProjects: projects.filter(p => names.includes(p.name)), workers });

test('mixed selected projects refuse parallel workers; default one worker stays safe', () => {
  assert.throws(() => assertSafeWorkers(config(['desktop-stateful', 'desktop'], 2)), /--workers=1/);
  assert.throws(() => assertSafeWorkers(config(['desktop-stateful', 'mobile-stateful'], 2)), /--workers=1/);
  assert.doesNotThrow(() => assertSafeWorkers(config(['desktop-stateful', 'desktop', 'mobile'], 1)));
});
test('filtered single-family repetitions may run with more workers', () => {
  assert.doesNotThrow(() => assertSafeWorkers(config(['desktop-stateful'], 4)));
  assert.doesNotThrow(() => assertSafeWorkers(config(['desktop', 'mobile'], 4)));
});
test('dependency closure catches batch terminal-chromium cascade and overlap', () => {
  assert.throws(() => assertSafeWorkers(config(['terminal-chromium', 'desktop'], 2)), /--workers=1/);
  const custom = { name: 'custom-view-tests', metadata: { presentationIsolation: true }, dependencies: ['setup'] };
  const chosen = config(['desktop'], 2);
  chosen.projects.push(custom); chosen.filteredProjects.push(custom);
  assert.throws(() => assertSafeWorkers(chosen), /--workers=1/);
});
test('hook-boundary diagnostic names state and both adjacent tests', () => {
  assert.throws(() => assertCleanBoundary(['ui_shell.alex'], 'a.spec.ts > predecessor', 'b.spec.ts > next'), /ui_shell.alex.*predecessor.*next/s);
  assert.throws(() => assertCleanBoundary(['ui_header.alex'], null, 'first test'), /beforeAll|initial/i);
  assert.doesNotThrow(() => assertCleanBoundary([], 'previous', 'current'));
});
test('import rule catches aliases, namespace imports and multiline shared suites; permits types', () => {
  assert.deepEqual(baseTestImports("import {\n test as defineTest, expect\n} from '@playwright/test';", 'suite.ts'), ['suite.ts']);
  assert.deepEqual(baseTestImports("import * as pw from '@playwright/test';", 'suite.ts'), ['suite.ts']);
  assert.deepEqual(baseTestImports("import type { Page } from '@playwright/test';", 'suite.ts'), []);
  assert.deepEqual(baseTestImports("import {test} from './fixtures';", 'suite.ts'), []);
  assert.deepEqual(baseTestImports("import {test} from '@playwright/test';", 'helpers.ts'), ['helpers.ts']);
  assert.deepEqual(baseTestImports("import { helper } from './helper'\nimport { /* suite */ test as run } from '@playwright/test'", 'suite.ts'), ['suite.ts']);
});
test('all executable e2e modules use the shared test fixture, including shared suites', () => {
  const root = new URL('../e2e/', import.meta.url);
  const bad = readdirSync(root, { recursive: true }).filter(file => file.endsWith('.ts') && !file.startsWith('.'))
    .flatMap(file => baseTestImports(readFileSync(new URL(file, root), 'utf8'), file));
  assert.deepEqual(bad, []);
});

test('actual CI and wiki screenshot commands collect every project family', () => {
  const web = new URL('../', import.meta.url);
  const workflow = readFileSync(new URL('../../.github/workflows/checks.yml', import.meta.url), 'utf8');
  assert.ok(workflow.indexOf('name: UI build') < workflow.indexOf('name: Browser isolation policy'),
    'Collection-based isolation checks require the UI build first');
  const ci = workflow.match(/run: (npx playwright test --project=[^\n]+)/)?.[1];
  assert.ok(ci, 'CI full browser gate command must be identified');
  const pkg = JSON.parse(readFileSync(new URL('package.json', web), 'utf8'));
  for (const command of [ci, pkg.scripts['screenshots:wiki']]) {
    const args = [...command.matchAll(/--project=([^\s]+)/g)].map(match => `--project=${match[1]}`);
    const result = spawnSync(process.execPath, ['node_modules/@playwright/test/cli.js', 'test', '--list', ...args], { cwd: web, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    for (const name of ['setup', 'view-registry', 'desktop-stateful', 'mobile-stateful', 'terminal-chromium', 'desktop', 'mobile']) {
      assert.ok(result.stdout.includes(`[${name}]`), `${command} omitted ${name}`);
    }
    assert.ok(command.includes('--workers=1'), 'Full gate must specify one worker');
  }
});
