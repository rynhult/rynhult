import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyLayout, pruneViews, openView, moveView, closeView, resizeColumns, effectiveColumns, referenceFor } from '../src/lib/workingLayout.ts';

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

test('project sections share a retained overview and update its destination', () => {
  const keys = referenceFor('/projects/91#project-keys');
  const ports = referenceFor('/projects/91?section=ports');
  assert.equal(keys.key, ports.key);
  let layout = openView(emptyLayout(), keys);
  layout = openView(layout, ports);
  assert.deepEqual(layout.columns[0].tabs, [keys.key]);
  assert.equal(layout.views[keys.key].href, '/projects/91?section=ports');
  assert.notEqual(referenceFor('/projects/92?section=ports').key, keys.key);
  assert.notEqual(referenceFor('/projects/91?tab=files').key, keys.key);
});


test('brief proposal review opens and closes inside the retained project overview', () => {
  const brief = referenceFor('/projects/91#project-brief');
  const review = referenceFor('/projects/91?brief_review=1#project-brief');
  const newer = referenceFor('/projects/91?brief_review=2#project-brief');
  assert.equal(review?.key, brief.key);
  assert.equal(newer?.key, brief.key);
  let layout = openView(openView(emptyLayout(), brief), review);
  assert.deepEqual(layout.columns[0].tabs, [brief.key]);
  assert.equal(layout.views[brief.key].href, review.href);
  layout = openView(layout, brief);
  assert.deepEqual(layout.columns[0].tabs, [brief.key]);
  assert.equal(layout.views[brief.key].href, brief.href);
  assert.equal(referenceFor('/projects/91?brief_review=1&brief_review=2'), null);
});

test('in-page diff references keep their working view and selected page state', () => {
  const changes = referenceFor('/sessions/demo?tab=changes');
  const line = referenceFor('/sessions/demo?tab=changes#diff-app-new-2');
  assert.equal(changes.key, line.key);
  const layout = openView(openView(emptyLayout(), changes), line);
  assert.deepEqual(layout.columns[0].tabs, [changes.key]);
  assert.equal(layout.views[changes.key].href, line.href);
  assert.notEqual(referenceFor('/sessions/demo?tab=activity').key, changes.key);
});

test('strip removes dead references and caps oldest first while preserving drafts, writers and active columns', () => {
  let layout = emptyLayout();
  for (let id = 0; id < 15; id++) layout = openView(layout, terminal(String(id)));
  const protectedKeys = new Set([terminal('0').key, terminal('1').key]);
  const pruned = pruneViews(layout, new Set([terminal('4').key]), protectedKeys);
  assert.equal(Object.keys(pruned.views).length, 12);
  for (const id of ['0', '1', '14']) assert.ok(pruned.views[terminal(id).key]);
  for (const id of ['2', '3', '4']) assert.equal(pruned.views[terminal(id).key], undefined);
  assert.equal(pruneViews(pruned, new Set(), protectedKeys), pruned);
  const dead = { key: 'dead', href: '', kind: 'unavailable', minWidth: 320, unrestorable: true };
  const selectedDead = openView(pruned, dead);
  assert.equal(pruneViews(selectedDead, new Set(), protectedKeys, 100), selectedDead);
  assert.equal(pruneViews(openView(selectedDead, terminal('14')), new Set(), protectedKeys).views.dead, undefined);
});

test('unresolved offline placeholders are kept until availability is known', () => {
  const view = { key: 'offline', href: '', kind: 'unavailable', minWidth: 320 };
  const layout = openView(emptyLayout(), view);
  assert.equal(pruneViews(layout, new Set(), new Set()), layout);
});

test('the twelve view cap applies separately to each column', () => {
  let layout = emptyLayout();
  for (let id = 0; id < 14; id++) layout = openView(layout, terminal(String(id)));
  layout = moveView(layout, terminal('0').key, 1);
  layout = moveView(layout, terminal('1').key, 1);
  assert.equal(pruneViews(layout, new Set(), new Set()), layout);
});

test('access loss preserves the active recovery view in every column', () => {
  let layout = openView(emptyLayout(), file(1, 'notes.txt'));
  layout = openView(layout, terminal('stopped'));
  layout = moveView(layout, file(1, 'notes.txt').key, 1);
  const unavailable = new Set(Object.keys(layout.views));
  assert.equal(pruneViews(layout, unavailable, new Set()), layout);
});
