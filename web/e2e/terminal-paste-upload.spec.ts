import { test, expect } from './fixtures';
import { AUTH_FILE, csrfHeaders, sessionWithChanges, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['admin_features', 'layouts'] });

for (const layout of ['new'] as const) test(`${layout}: file paste/drop, unchanged text, read-only refusal and no Enter`, async ({ page, context }) => {
  const id = await sessionWithChanges(page, 'terminal-upload');
  const frames: string[] = [];
  const uploadIds: string[] = [];
  page.on('websocket', socket => {
    const uploadId = new URL(socket.url()).searchParams.get('upload_id');
    if (uploadId) uploadIds.push(uploadId);
    socket.on('framesent', frame => {
      try { const value = JSON.parse(String(frame.payload)); if (value.t === 'i') frames.push(value.d); } catch { /* binary output */ }
    });
  });
  try {
    await page.goto(`/sessions/${id}`);
    if (layout === 'new') {
      await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    } else await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
    const input = page.locator('.xterm-helper-textarea');
    async function gesture(kind: 'paste' | 'drop', name: string, type: string) {
      await input.evaluate((element, values) => {
        const transfer = new DataTransfer();
        transfer.items.add(new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], values.name, { type: values.type }));
        element.dispatchEvent(values.kind === 'paste' ? new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }) : new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }));
      }, { kind, name, type });
    }
    await gesture('paste', 'image.png', 'image/png');
    await expect(page.getByRole('status').filter({ hasText: 'Upload done.' })).toBeVisible();
    await expect.poll(() => frames.filter(frame => frame.includes('.rynhult/uploads/')).length).toBe(1);
    await gesture('drop', 'notes with spaces.txt', 'text/plain');
    await expect.poll(() => frames.filter(frame => frame.includes('.rynhult/uploads/')).length).toBe(2);
    expect(frames.every(frame => !/[\r\n]/.test(frame))).toBe(true);
    expect(frames.join('')).toContain('notes-with-spaces.txt');
    await input.evaluate(element => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['one'], 'one.txt'));
      transfer.items.add(new File(['two'], 'two.txt'));
      element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
    });
    await expect.poll(() => frames.filter(frame => frame.includes('.rynhult/uploads/')).length).toBe(3);
    expect(frames.filter(frame => frame.includes('.rynhult/uploads/'))[2].split(' ')).toHaveLength(2);
    await input.evaluate(element => {
      const transfer = new DataTransfer(); transfer.setData('text/plain', 'plain text');
      element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
    });
    await expect.poll(() => frames.join('')).toContain('plain text');
    expect(frames.every(frame => !/[\r\n]/.test(frame))).toBe(true);
    const uploadUrl = `/api/files/session/${id}/upload`;
    for (const unsafe of ['', undefined, '.rynhult/uploads/a\r', '../escape', '.rynhult/uploads/a b', '.rynhult/uploads/$(touch)', '.rynhult/uploads/a\n', '/absolute', '.rynhult/uploads/é']) {
      const before = frames.length;
      await page.route(`**${uploadUrl}`, route => route.fulfill({ json: { files: [
        { path: '.rynhult/uploads/safe.png' }, { path: unsafe },
      ] } }));
      try {
        await gesture('paste', 'unsafe.png', 'image/png');
        await expect(page.getByRole('status').filter({ hasText: 'unsafe path. No paths inserted.' })).toBeVisible();
        expect(frames).toHaveLength(before);
      } finally { await page.unroute(`**${uploadUrl}`); }
    }
    const observer = await context.newPage();
    const observerUploadIds: string[] = [];
    observer.on('websocket', socket => {
      const uploadId = new URL(socket.url()).searchParams.get('upload_id');
      if (uploadId) observerUploadIds.push(uploadId);
    });
    try {
      await observer.goto(`/sessions/${id}`);
      await expect(observer.getByText(layout === 'new' ? 'Read-only terminal' : /Read-only ·/, { exact: layout === 'new' })).toBeVisible();
      let uploads = 0;
      observer.on('request', request => { if (request.url().endsWith('/upload')) uploads++; });
      await observer.locator('.xterm-helper-textarea').evaluate(element => {
        const transfer = new DataTransfer(); transfer.items.add(new File(['observer'], 'blocked.txt'));
        element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
      });
      await expect(observer.getByRole('status').filter({ hasText: 'Read-only terminal:' })).toBeVisible();
      expect(uploads).toBe(0);
      await expect.poll(() => observerUploadIds.length).toBeGreaterThan(0);
      expect(uploadIds).not.toContain(observerUploadIds[0]);
      const refused = await observer.request.post(uploadUrl, { headers: await csrfHeaders(observer), data: {
        terminal_upload: observerUploadIds[0], files: [{ path: 'direct-observer.txt', data: 'aGk=' }],
      } });
      expect(refused.status()).toBe(403);
      expect(await refused.text()).toContain('Read-only terminal');
    } finally { await observer.close(); }
  } finally {
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
});
