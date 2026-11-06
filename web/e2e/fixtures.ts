import { test as base, expect, type BrowserContext } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { AUTH_FILE, BASE } from './fixture-paths';
import { assertCleanBoundary, presentationProject } from './isolation-policy';

export * from '@playwright/test';
type PresentationGroup = 'admin_features' | 'ui_shell' | 'setup_level' | 'ui_header' | 'theme' | 'layouts';

/** Explicit choice for additional people created by a journey. */
export async function chooseLayout(context: BrowserContext, enabled: boolean) {
  const { origin } = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  const csrf = (await context.cookies()).find(cookie => cookie.name === 'acc_csrf' || cookie.name === '__Host-acc_csrf')!.value;
  const response = await context.request.put(`${origin}/api/preferences/ui-shell`, {
    headers: { 'X-ACC-CSRF': csrf, Origin: origin }, data: { enabled },
  });
  expect(response.ok(), 'Save personal layout choice').toBe(true);
}

/** Declare temporary settings owned by this test; everything else is a leak. */
export const test = base.extend<{ signedInLayout: 'classic' | 'new' | 'unchosen'; _signedInLayout: void; presentationChanges: PresentationGroup[]; _presentationIsolation: void;
  isolatedOwner: { username: string; secret: string };
  otherOwner: BrowserContext;
  _drainRoutes: void;
  arrangeTeam: (action: 'member' | 'history', lead: string, helper: string) => Promise<void> }>({
  signedInLayout: ['classic', { option: true }],
  // Run after the presentation reset and restore before its leak check. Use a
  // separate signed-in control so logout, member cookies and route mocks cannot
  // prevent cleanup. The unchosen option also leaves isolated owners untouched.
  _signedInLayout: [async ({ playwright, signedInLayout, presentationChanges, _presentationIsolation }, use, testInfo) => {
    if (testInfo.project.name === 'setup' || testInfo.project.name === 'view-registry' || signedInLayout === 'unchosen') {
      await use(); return;
    }
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    const signedInControl = () => playwright.request.newContext({ baseURL: fixture.origin, storageState: AUTH_FILE });
    let control = await signedInControl();
    try {
      const original = await control.get('/api/preferences/ui-shell');
      expect(original.ok(), 'Read signed-in layout').toBe(true);
      const saved = await original.json();
      const enabled = signedInLayout === 'new';
      const choose = async (value: boolean) => {
        const cookies = (await control.storageState()).cookies;
        const csrf = cookies.find(cookie => cookie.name === 'acc_csrf' || cookie.name === '__Host-acc_csrf')!.value;
        const headers = { 'X-ACC-CSRF': csrf, Origin: fixture.origin };
        const response = await control.put('/api/preferences/ui-shell', { headers, data: { enabled: value } });
        expect(response.ok(), 'Save signed-in layout').toBe(true);
      };
      await choose(enabled);
      try { await use(); }
      finally {
        // Authentication journeys save replacement sign-ins in AUTH_FILE in
        // afterEach (recovery can revoke every earlier sign-in).
        await control.dispose();
        control = await signedInControl();
        try {
          if (!presentationChanges.includes('ui_shell')) {
            expect((await (await control.get('/api/preferences/ui-shell')).json()).enabled,
              'Undeclared signed-in layout change').toBe(enabled);
          }
        } finally {
          // Ordinary desktop/mobile tests may share this owner concurrently.
          // Keep their common classic baseline instead of restoring the first
          // test's unchosen default while another classic journey is running.
          await choose(presentationProject(testInfo.project) ? saved.enabled : false);
        }
      }
    } finally { await control.dispose(); }
  }, { auto: true }],
  arrangeTeam: async ({ context }, use) => {
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    await use(async (action, lead, helper) => {
      const response = await context.request.post(`/e2e-test/teams/${lead}/fixtures/${action}/${helper}`, {
        headers: { 'x-e2e-isolation': fixture.isolation_token },
      });
      expect(response.ok(), await response.text()).toBe(true);
    });
  },
  // Opt in before navigating. BrowserContext.request shares this context's cookie
  // jar, so the real login/reauth/logout paths operate on this test's owner only.
  isolatedOwner: async ({ context, browser, signedInLayout }, use) => {
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    const response = await context.request.post('/e2e-test/identity', {
      headers: { 'x-e2e-isolation': fixture.isolation_token },
    });
    expect(response.ok(), 'Disposable owner creation failed').toBe(true);
    if (signedInLayout === 'unchosen') { await use(await response.json()); return; }
    const cleanup = await browser.newContext({ storageState: await context.storageState() });
    try {
      await chooseLayout(context, signedInLayout === 'new');
      await use(await response.json());
    } finally {
      try { await chooseLayout(cleanup, true); }
      finally { await cleanup.close(); }
    }
  },
  // Different people may share the owners' default agent account. Never borrow
  // a named member account from another spec just to exercise person fencing.
  otherOwner: async ({ browser }, use) => {
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    const other = await browser.newContext({ baseURL: fixture.origin });
    try {
      const response = await other.request.post('/e2e-test/identity', {
        headers: { 'x-e2e-isolation': fixture.isolation_token },
      });
      expect(response.ok(), 'Disposable second owner creation failed').toBe(true);
      await use(other);
    } finally { await other.close(); }
  },
  // Opt in only for finite interceptors. Deliberately held loading-state routes
  // must retain their own release/abort policy. Depend on page so draining runs
  // before Playwright tears it down and rejects an in-flight route.fetch().
  _drainRoutes: async ({ page, context }, use) => {
    void page;
    try { await use(); }
    finally {
      await Promise.all(context.pages().map(target => target.unrouteAll({ behavior: 'wait' })));
      await context.unrouteAll({ behavior: 'wait' });
    }
  },
  presentationChanges: [[], { option: true }],
  _presentationIsolation: [async ({ playwright, presentationChanges }, use, testInfo) => {
    if (!presentationProject(testInfo.project)) {
      await use(); return;
    }
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    // Separate from the tested browser: expiry, sign-out, member changes and
    // page.route mocks must not disable cleanup or conceal the real state.
    const control = await playwright.request.newContext({
      baseURL: fixture.origin, extraHTTPHeaders: { 'x-e2e-isolation': fixture.isolation_token },
    });
    const reset = async (groups?: PresentationGroup[], test?: string) => {
      const response = await control.post('/e2e-test/presentation', { data: { groups, test } });
      expect(response.ok(), 'Disposable presentation reset failed').toBe(true);
      return await response.json() as { changed: string[]; before: string[]; previous_test: string | null };
    };
    try {
      const current = `${testInfo.project.name}: ${testInfo.titlePath.join(' > ')}`;
      const initial = await reset(undefined, current);
      expect(initial.changed, 'Presentation defaults could not be established').toEqual([]);
      assertCleanBoundary(initial.before, initial.previous_test, current);
      try { await use(); }
      finally {
        // The fixture owns only declared temporary changes. Detect other drift
        // before the unconditional reset, so it fails the responsible test.
        let leaked: string[] = [];
        try { leaked = (await reset(presentationChanges)).changed; }
        finally { expect((await reset()).changed, 'Presentation cleanup failed').toEqual([]); }
        expect(leaked, `Unowned shared presentation state leaked: ${leaked.join(', ')}`).toEqual([]);
      }
    } finally { await control.dispose(); }
  }, { auto: true }],
});
