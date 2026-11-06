import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { Traffic, median, polling } from './metrics.mjs';

test('asset bytes are counted once, including partially received streams at the cutoff', () => {
  const traffic = new Traffic();
  traffic.request({ requestId: 'js', type: 'Script', request: { url: 'http://localhost/a.js', method: 'GET' } }, 0);
  traffic.response({ requestId: 'js', response: { status: 200 } });
  traffic.data({ requestId: 'js', dataLength: 1000, encodedDataLength: 400 }, 1);
  traffic.finish({ requestId: 'js', encodedDataLength: 550 }, 2);
  traffic.request({ requestId: 'sse', type: 'EventSource', request: { url: 'http://localhost/api/events/stream', method: 'GET' } }, 100);
  traffic.data({ requestId: 'sse', dataLength: 80, encodedDataLength: 80 }, 101);
  traffic.request({ requestId: 'late', type: 'Stylesheet', request: { url: 'http://localhost/late.css', method: 'GET' } }, 10001);
  traffic.finish({ requestId: 'late', encodedDataLength: 9999 }, 10002);
  assert.deepEqual(traffic.window(0, 10000), {
    requests: 2, completed: 1, failed: 0, wire_bytes: 630, decoded_body_bytes: 1080,
    js_wire_bytes: 550, css_wire_bytes: 0, js_decoded_bytes: 1000, css_decoded_bytes: 0,
    by_path: { '/a.js': { requests: 1, wire_bytes: 550 }, '/api/events/stream': { requests: 1, wire_bytes: 80 } },
  });
});

test('redirected requests keep both network transactions; window boundaries exclude later polling', () => {
  const traffic = new Traffic();
  const request = { requestId: 'a', type: 'Fetch', request: { url: 'http://localhost/api/old', method: 'GET' } };
  traffic.request(request, 1);
  traffic.request({ ...request, redirectResponse: { encodedDataLength: 120, status: 302 }, request: { ...request.request, url: 'http://localhost/api/new' } }, 2);
  traffic.finish({ requestId: 'a', encodedDataLength: 200 }, 3);
  assert.equal(traffic.window(0, 10).requests, 2);
  assert.equal(traffic.window(0, 10).wire_bytes, 320);
  assert.deepEqual(polling([
    { time: 0, url: '/api/health', method: 'GET', type: 'Fetch' },
    { time: 1000, url: '/api/health', method: 'GET', type: 'Fetch' },
    { time: 2000, url: '/api/events/stream', method: 'GET', type: 'EventSource' },
    { time: 3000, url: '/assets/app.js', method: 'GET', type: 'Script' },
    { time: 60000, url: '/api/health', method: 'GET', type: 'Fetch' },
  ], 0, 60000), { requests: 2, requests_per_minute: 2, streams: 1, by_path: { '/api/health': 2 } });
});

test('missing measurements remain missing rather than becoming a false zero', () => {
  assert.equal(median([300, 100, 500, 200, 400]), 300);
  assert.equal(median([null, undefined]), null);
  assert.equal(median([0, 2, 4, 6, 8]), 4);
});

test('a response finishing after the cutoff contributes only bytes received before the cutoff', () => {
  const traffic = new Traffic();
  traffic.request({ requestId: 'slow', type: 'Script', request: { url: 'http://localhost/slow.js', method: 'GET' } }, 0);
  traffic.data({ requestId: 'slow', dataLength: 500, encodedDataLength: 200 }, 1000);
  traffic.data({ requestId: 'slow', dataLength: 1500, encodedDataLength: 600 }, 11000);
  traffic.finish({ requestId: 'slow', encodedDataLength: 950 }, 12000);
  assert.equal(traffic.window(0, 10000).wire_bytes, 200);
  assert.equal(traffic.window(0, 10000).js_decoded_bytes, 500);
  assert.equal(traffic.window(0, 10000).completed, 0);
});

test('WebSocket payload counts distinguish UTF-8 text, binary frames and directions', async () => {
  const { sockets } = await import('./metrics.mjs');
  assert.deepEqual(sockets([
    { kind: 'open', time: 0 },
    { kind: 'received', time: 1, opcode: 1, payload: 'Hi✓' },
    { kind: 'received', time: 2, opcode: 2, payload: 'AQID' },
    { kind: 'sent', time: 3, opcode: 1, payload: 'ab' },
    { kind: 'received', time: 10000, opcode: 1, payload: 'excluded' },
  ], 0, 10000), { connections: 1, received_payload_bytes: 8, sent_payload_bytes: 2 });
});

test('resume refuses smoke, changed builds and duplicate completed observations', async () => {
  const { resumeTrials } = await import('./metrics.mjs');
  const row = { projects: 10, sessions: 5, shell: 'old', run: 1, memory_after_ms: 300001,
    polling: { visible: { observed_visibility: 'visible' }, hidden: { observed_visibility: 'hidden' } },
    memory_5m: { js_heap_used_bytes: 123 }, page_errors: [],
    pages: Object.fromEntries(['Projects', 'Overview', 'Session'].map(k => [k, { failed: 0, interactive_proxy_ms: 100 }])) };
  const source = { smoke: false, runs_per_condition: 5, session_dwell_ms: 300000, cold_window_ms: 10000,
    polling_window_ms: 60000, build_manifest_sha256: 'same-build', revision: 'original', trials: [row] };
  assert.deepEqual(resumeTrials(source, 'same-build').map(r => r.harness_revision), ['original']);
  assert.throws(() => resumeTrials({ ...source, smoke: true }, 'same-build'), /smoke/i);
  assert.throws(() => resumeTrials(source, 'different-build'), /build/i);
  assert.throws(() => resumeTrials({ ...source, trials: [row, row] }, 'same-build'), /duplicate/i);
  assert.throws(() => resumeTrials({ ...source, trials: [{ ...row, memory_after_ms: 5000 }] }, 'same-build'), /memory/i);
  const canceled = { ...row, pages: { ...row.pages, Projects: { failed: 1, interactive_proxy_ms: 100,
    expected_cancellations: [{ url: '/rynhult-mark-dark.svg', type: 'Image', reason: 'net::ERR_ABORTED', canceled: true }] } } };
  assert.equal(resumeTrials({ ...source, trials: [canceled] }, 'same-build').length, 1);
  assert.throws(() => resumeTrials({ ...source, trials: [{ ...canceled, pages: { ...canceled.pages, Projects: { ...canceled.pages.Projects, failed: 2 } } }] }, 'same-build'), /cold-route/i);
});

test('only explicit canceled paired-theme brand images are nonfatal', async () => {
  const { expectedCancellation } = await import('./metrics.mjs');
  const image = { url: '/rynhult-mark-dark.svg', type: 'Image', reason: 'net::ERR_ABORTED', canceled: true };
  assert.equal(expectedCancellation(image), true);
  assert.equal(expectedCancellation({ ...image, url: '/rynhult-mark-light.svg' }), true);
  assert.equal(expectedCancellation({ ...image, url: '/api/projects' }), false);
  assert.equal(expectedCancellation({ ...image, url: '/other-image.svg' }), false);
  assert.equal(expectedCancellation({ ...image, type: 'Script' }), false);
  assert.equal(expectedCancellation({ ...image, canceled: false }), false);
  assert.equal(expectedCancellation({ ...image, reason: 'net::ERR_CONNECTION_RESET' }), false);
});
