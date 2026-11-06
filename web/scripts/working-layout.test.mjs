import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyLayout, openView, moveView, closeView, resizeColumns, effectiveColumns, referenceFor } from '../src/lib/workingLayout.ts';

const terminal = (id) => referenceFor(`/sessions/${id}`);
const file = (id, name = 'src/app.ts') => referenceFor(`/projects/${id}?tab=files&file=${encodeURIComponent(name)}`);

test('same resource focuses its existing tab; identically named files and sessions keep distinct identities', () => {
  let layout = openView(emptyLayout(), terminal('one'));
  layout = openView(layout, terminal('two'));
  layout = openView(layout, file(1)); layout = openView(layout, file(2));
  assert.equal(Object.keys(layout.views).length, 4);
  layout = openView(layout, terminal('one'));
  assert.equal(Object.keys(layout.views).length, 4);
  assert.equal(layout.columns[0].active, 'session:one:terminal');
  assert.equal(layout.views['file:project:1:src/app.ts'].href, '/projects/1?tab=files&file=src%2Fapp.ts');
});

test('moving and closing a placement preserve the other references and leave an empty column usable', () => {
  let layout = openView(emptyLayout(), terminal('one'));
  layout = openView(layout, file(1));
  layout = moveView(layout, 'session:one:terminal', 2);
  assert.equal(layout.columns.length, 3);
  assert.deepEqual(layout.columns.map(c => c.tabs), [['file:project:1:src/app.ts'], [], ['session:one:terminal']]);
  assert.equal(layout.focused, 2);
  assert.equal(layout.views['session:one:terminal'].href, '/sessions/one');
  layout = closeView(layout, 'session:one:terminal');
  assert.deepEqual(layout.columns[2].tabs, []); assert.equal(layout.columns[2].active, null);
  assert.equal(Object.keys(layout.views).length, 1);
});

test('narrow layouts show one complete column without changing saved placements; per-view minima refuse false three-column availability', () => {
  let layout = openView(emptyLayout(), terminal('one'));
  layout = moveView(openView(layout, file(1)), file(1).key, 1);
  layout = moveView(openView(layout, referenceFor('/projects/1?tab=preview')), 'page:/projects/1?tab=preview', 2);
  const before = JSON.stringify(layout);
  assert.equal(effectiveColumns(layout, 390), 1);
  assert.equal(effectiveColumns(layout, 1000), 1);
  assert.equal(effectiveColumns(layout, 1200), 3);
  assert.equal(JSON.stringify(layout), before);
  const resized = resizeColumns(layout, 0, -10000, 1200);
  const widths = resized.columns.map(c => c.width * (1200 - 16));
  assert.ok(widths[0] >= 320); assert.ok(widths[1] >= 360); assert.ok(widths[2] >= 400);
  assert.ok(Math.abs(resized.columns.reduce((n, c) => n + c.width, 0) - 1) < 0.00001);
});

test('layout parsing bounds input and rejects forged resource keys, external URLs and authority or draft fields', () => {
  const valid = openView(emptyLayout(), file(1, 'notes #1.md'));
  for (const href of ['//example.com/', 'javascript:alert(1)', '/sessions/one?take=1', '/projects/1?tab=files&file=../secret', '/projects/1?tab=files&file=/etc/passwd']) {
    assert.equal(referenceFor(href), null);
  }
});

test('workspace landing without a session follows the explanatory page instead of a session access check', () => {
  assert.equal(referenceFor('/workspaces/cedar/files'), null);
  assert.equal(referenceFor('/workspaces/cedar/files?session=abc&file=notes.txt').kind, 'file');
});
