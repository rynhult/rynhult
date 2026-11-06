import { test } from 'node:test';
import assert from 'node:assert/strict';

test('UI-14 a seventeenth admitted source reports capacity, not lost authority', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(options.credentials, 'same-origin');
    assert.ok(url === '/api/auth/state' || /^\/api\/sessions\/session\d+$/.test(url));
    return Response.json(url === '/api/auth/state' ? {authenticated: true, username: 'casey'} : {});
  };
  const ports = [], inboxes = [];
  try {
    await import('../src/lib/chat-worker.ts');
    for (let i = 0; i < 17; i++) {
      const inbox = []; inboxes.push(inbox);
      const port = {postMessage: value => inbox.push(value), start() {}, close() {}};
      ports.push(port);
      globalThis.onconnect({ports: [port]});
      await port.onmessage({data: {type: 'subscribe', sessionId: `session${i}`, scope: 'scope', visible: true, active: false}});
    }
    assert.equal(inboxes.slice(0,16).every(inbox => inbox.some(message => message.type === 'lead')), true);
    assert.equal(inboxes[16][0].type, 'capacity');
    assert.equal(inboxes[16].some(message => message.type === 'denied'), false);
  } finally {
    for (const port of ports) await port.onmessage({data: {type: 'close'}});
    globalThis.fetch = originalFetch;
    delete globalThis.onconnect;
  }
});
