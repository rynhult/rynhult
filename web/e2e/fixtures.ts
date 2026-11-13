import { test as base, expect, type BrowserContext } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { BASE } from './fixture-paths';
import { assertCleanBoundary, presentationProject } from './isolation-policy';

export * from '@playwright/test';
type PresentationGroup = 'admin_features' | 'setup_level' | 'ui_header' | 'theme' | 'layouts';

/** Declare temporary settings owned by this test; everything else is a leak. */
export const test = base.extend<{ presentationChanges: PresentationGroup[]; _presentationIsolation: void;
  isolatedOwner: { username: string; secret: string };
  otherOwner: BrowserContext;
  _drainRoutes: void;
  createTeamHelper: (lead: string) => Promise<string>;
  arrangeTeam: (action: 'member' | 'history', lead: string, helper: string) => Promise<void> }>({
  createTeamHelper: async ({ context }, use) => {
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    await use(async lead => {
      const response = await context.request.post(`/e2e-test/teams/${lead}/helpers`, {
        headers: { 'x-e2e-isolation': fixture.isolation_token },
      });
      expect(response.ok(), await response.text()).toBe(true);
      return (await response.json()).id;
    });
  },
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
  isolatedOwner: async ({ context }, use) => {
    const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
    const response = await context.request.post('/e2e-test/identity', {
      headers: { 'x-e2e-isolation': fixture.isolation_token },
    });
    expect(response.ok(), 'Disposable owner creation failed').toBe(true);
    await use(await response.json());
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
        // The mounted frame owns its automatic layout writes; tests declare
        // other temporary changes. Detect other drift
        // before the unconditional reset, so it fails the responsible test.
        let leaked: string[] = [];
        try { leaked = (await reset([...new Set<PresentationGroup>([...presentationChanges, 'layouts'])])).changed; }
        finally { expect((await reset()).changed, 'Presentation cleanup failed').toEqual([]); }
        expect(leaked, `Unowned shared presentation state leaked: ${leaked.join(', ')}`).toEqual([]);
      }
    } finally { await control.dispose(); }
  }, { auto: true }],
});
