import { test as base, expect, type Page } from './fixtures';
import { AUTH_FILE, csrfHeaders, info } from './helpers';

const headerDefaults = {
  order: ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'], hidden: [],
};
export interface ShellState {
  setup: { level: number | null; dismissed: boolean };
  header: { order: string[]; hidden: string[] };
}
async function read(page: Page, path: string) {
  const response = await page.request.get(path);
  expect(response.ok(), path).toBeTruthy();
  return response.json();
}
async function put(page: Page, path: string, data: unknown) {
  const response = await page.request.put(path, { headers: await csrfHeaders(page), data });
  expect(response.ok(), `${path}: ${await response.text()}`).toBeTruthy();
}
export async function readShellState(page: Page): Promise<ShellState> {
  const [setup, header] = await Promise.all([
    read(page, '/api/preferences/setup-level'), read(page, '/api/preferences/ui-header'),
  ]);
  return { setup: { level: setup.level, dismissed: setup.dismissed }, header };
}
export async function restoreShellState(page: Page, original: ShellState) {
  await put(page, '/api/preferences/ui-header', original.header);
  await put(page, '/api/preferences/setup-level', original.setup);
  expect(await readShellState(page)).toEqual(original);
}

/** Independent shell journeys own their presentation state. The separate owner
 * context survives tests that replace their page's cookies with a member's.
 */
export const test = base.extend<{ shellState: void }>({
  // Mounting the new shell's P2 views persists personal arrangements.
  presentationChanges: [['layouts'], { option: true }],
  shellState: [async ({ browser, page, _presentationIsolation }, use) => {
    const owner = await browser.newContext({ baseURL: info().origin, storageState: AUTH_FILE });
    const control = await owner.newPage();
    try {
      const original = await readShellState(control);
      try {
        await put(control, '/api/preferences/setup-level', { level: 0, dismissed: true });
        await put(control, '/api/preferences/ui-header', headerDefaults);
        await page.bringToFront();
        await use();
      } finally {
        // Fetch/fulfill handlers must finish before Playwright disposes their context.
        try { if (!page.isClosed()) await page.unrouteAll({ behavior: 'wait' }) }
        finally {
          // Pairing/recovery can revoke every earlier owner sign-in. Those
          // journeys save a replacement in AUTH_FILE before fixture teardown.
          const cleanup = await browser.newContext({ baseURL: info().origin, storageState: AUTH_FILE });
          try { await restoreShellState(await cleanup.newPage(), original) }
          finally { await cleanup.close() }
        }
      }
    } finally { await owner.close() }
  }, { auto: true }],
});
