/** The P2 library against the disposable API, before the shell consumes it. */
import { expect } from '@playwright/test';
import { test } from './shell-state';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import type { PersonalLayoutStore, LayoutScope } from '../src/lib/layoutStore';
import { AUTH_FILE, csrfHeaders, gitProject, verifyAgain } from './helpers';

declare global {
  interface Window {
    layoutTest: { PersonalLayoutStore: typeof PersonalLayoutStore; api: typeof import('../src/lib/api').api };
    layoutStore: PersonalLayoutStore;
  }
}
const bundle = 'window.layoutTest = {};' + ['draftIdentity', 'layoutStore', 'api'].map(name => {
  const source = readFileSync(new URL(`../src/lib/${name}.ts`, import.meta.url), 'utf8');
  const js = stripTypeScriptTypes(source, { mode: 'transform' }).replace(/^import .* from .*;\n/gm, '').replace(/^export /gm, '');
  const exports = name === 'api' ? 'Object.assign(exports, {api, ApiError});' : name === 'draftIdentity' ? 'Object.assign(exports, {draftIdentity, setDraftIdentity});' : 'exports.PersonalLayoutStore = PersonalLayoutStore;';
  const imports = name === 'api' ? 'const {draftIdentity, setDraftIdentity} = window.layoutTest;' : '';
  return `(function(exports){${imports}${js}\n${exports}})(window.layoutTest);`;
}).join('');

test.use({ presentationChanges: ['layouts'], storageState: AUTH_FILE });
const one = (id: string) => ({ schema_version: 2 as const, views: [{ id, kind: 'page' as const }], columns: [{ tabs: [id], active: id, width: 1 }], focused: 0, filled: null });
test('personal arrangement survives offline reload and requires an explicit CAS conflict choice', async ({ page, context }) => {
  await page.goto('/projects');
  const project = await gitProject(page, 'layout-library');
  const path = `/api/projects/${project.id}/layouts/desktop/wide`;
  const initial = await (await page.request.get(path)).json();
  await page.evaluate(bundle);
  const open = async (scope: LayoutScope) => page.evaluate(async identity => {
    window.layoutStore = new window.layoutTest.PersonalLayoutStore(identity, window.layoutTest.api);
    return window.layoutStore.load();
  }, scope);
  expect((await open(initial.scope)).status).toBe('ready');
  await page.evaluate(one => window.layoutStore.stage(one), one('work'));
  expect((await page.evaluate(() => window.layoutStore.save())).status).toBe('saved');
  await page.evaluate(one => window.layoutStore.stage(one), one('local'));
  const other = await page.request.put(path, { headers: await csrfHeaders(page), data: {
    expected_revision: 1, layout: one('other-device'),
  } });
  expect(other.ok(), await other.text()).toBe(true);
  const conflict = await page.evaluate(() => window.layoutStore.save());
  expect(conflict.status).toBe('conflict');
  expect(conflict.server?.layout?.views[0].id).toBe('other-device');
  expect(conflict.pending?.layout?.views[0].id).toBe('local');
  await page.evaluate(() => window.layoutStore.resolve('local'));
  expect((await page.evaluate(() => window.layoutStore.save())).status).toBe('saved');
  await page.evaluate(one => window.layoutStore.stage(one), one('offline'));
  await context.setOffline(true);
  const offline = await page.evaluate(() => window.layoutStore.save());
  expect(offline.status).toBe('offline');
  expect(offline.durable).toBe(true);
  await context.setOffline(false);
  await page.reload();
  await page.evaluate(bundle);
  const restored = await open(initial.scope);
  expect(restored.pending?.layout?.views[0].id).toBe('offline');
  expect(restored.server?.revision).toBe(3); // load never silently writes
  expect((await page.evaluate(() => window.layoutStore.save())).server?.revision).toBe(4);
});
