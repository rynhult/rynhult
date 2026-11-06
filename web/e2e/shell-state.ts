import { test as base, expect, type Page } from './fixtures';
import { AUTH_FILE, csrfHeaders, info, verifyAgain } from './helpers';

const headerDefaults = {
  order: ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'], hidden: [],
};
export interface ShellState {
  feature: boolean;
  shell: { enabled: boolean };
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
  const [features, shell, setup, header] = await Promise.all([
    read(page, '/api/admin-features'), read(page, '/api/preferences/ui-shell'),
    read(page, '/api/preferences/setup-level'), read(page, '/api/preferences/ui-header'),
  ]);
  return { feature: features.features.find((row: { id: string; enabled: boolean }) => row.id === 'new_shell')?.enabled ?? true,
    shell: { enabled: shell.enabled }, setup: { level: setup.level, dismissed: setup.dismissed }, header };
}
export async function setShellFeature(page: Page, enabled: boolean) {
  const change = async () => {
    const state = await read(page, '/api/admin-features');
    if ((state.features.find((row: { id: string; enabled: boolean }) => row.id === 'new_shell')?.enabled ?? true) === enabled) return;
    return page.request.put('/api/admin-features/new_shell', {
      headers: await csrfHeaders(page), data: { enabled, revision: state.revision },
    });
  };
  let response = await change();
  if (response?.status() === 403 && enabled) { await verifyAgain(page); response = await change(); }
  if (response) expect(response.ok(), await response.text()).toBeTruthy();
}
export async function restoreShellState(page: Page, original: ShellState) {
  await put(page, '/api/preferences/ui-header', original.header);
  await put(page, '/api/preferences/setup-level', original.setup);
  await put(page, '/api/preferences/ui-shell', original.shell);
  await setShellFeature(page, original.feature);
  expect(await readShellState(page)).toEqual(original);
}

/** Independent shell journeys own their presentation state. The separate owner
 * context survives tests that replace their page's cookies with a member's.
 */
export const test = base.extend<{ shellState: void; shellLayout: 'new' | 'standard' }>({
  // Mounting the new shell's P2 views persists personal arrangements.
  presentationChanges: [['layouts'], { option: true }],
  shellLayout: ['new', { option: true }],
  shellState: [async ({ browser, page, shellLayout, _presentationIsolation, _signedInLayout }, use) => {
    const owner = await browser.newContext({ baseURL: info().origin, storageState: AUTH_FILE });
    const control = await owner.newPage();
    try {
      const original = await readShellState(control);
      try {
        if (shellLayout === 'new') {
          await setShellFeature(control, true);
          await put(control, '/api/preferences/setup-level', { level: 0, dismissed: true });
          await put(control, '/api/preferences/ui-header', headerDefaults);
        }
        // Standard-layout journeys must not inherit the preceding spec's opt-in.
        // Leave the installation feature unchanged: the person's preference is enough.
        await put(control, '/api/preferences/ui-shell', { enabled: shellLayout === 'new' });
        expect((await read(control, '/api/preferences/ui-shell')).enabled).toBe(shellLayout === 'new');
        await page.bringToFront();
        await use();
      } finally {
        // Fetch/fulfill handlers must finish before Playwright disposes their context.
        try { if (!page.isClosed()) await page.unrouteAll({ behavior: 'wait' }); }
        finally {
          // Pairing/recovery can revoke every earlier owner sign-in. Those
          // journeys save a replacement in AUTH_FILE before fixture teardown.
          const cleanup = await browser.newContext({ baseURL: info().origin, storageState: AUTH_FILE });
          try { await restoreShellState(await cleanup.newPage(), original); }
          finally { await cleanup.close(); }
        }
      }
    } finally { await owner.close(); }
  }, { auto: true }],
});
