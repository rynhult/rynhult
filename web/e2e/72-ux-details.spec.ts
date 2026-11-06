import { gotoLoadedPage } from './loaded-page';
import { expect, test } from "./fixtures";
import { AUTH_FILE, expectAxeClean, fileRow, gitProject, freshStep, loadSecret, PASSWORD, totp, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Every repetition uses the same real owner authenticator. This file belongs to
// the stateful project: TOTP counters and policy registry revisions are shared.
async function reviewDraft(page: import('@playwright/test').Page) {
  const waitPreview = () => page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/security/changes/preview');
  const pending = waitPreview();
  await page.locator('#security').getByRole('button', { name: 'Review changes', exact: true }).click();
  const response = await pending;
  expect([200, 403]).toContain(response.status());
  expect(await response.finished()).toBeNull();
  if (response.status() === 403) {
    test.slow(); // Real one-use authenticator counters can require the next 30s step.
    const verify = page.getByRole('dialog', { name: 'Verify your sign-in', exact: true });
    await freshStep(page);
    await verify.getByLabel('Verification password').fill(PASSWORD);
    await verify.getByLabel('Verification authenticator code', { exact: true }).fill(totp(loadSecret()));
    const verified = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/auth/passkeys/reauth');
    const preview = waitPreview();
    await verify.getByRole('button', { name: 'Verify with password and authenticator', exact: true }).click();
    expect((await verified).ok()).toBe(true);
    expect((await preview).ok()).toBe(true);
    expect(await (await preview).finished()).toBeNull();
  }
  const review = page.getByRole('dialog', { name: 'Review Security changes', exact: true });
  await expect(review).toBeVisible();
  return review;
}


for (const width of [1440, 390]) {
  test.describe(`${width}px detail refinements`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`Files collapses only accepted phone selections and keeps cancelled drafts in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const project = await gitProject(page, `files-${width}-${theme}`);
        expect((await page.request.post(`/api/files/project/${project.id}`, { headers: project.headers,
          data: { action: "create", path: "notes.md", content: "# Notes\n\nOriginal draft." } })).ok()).toBe(true);
        await page.goto(`/projects/${project.id}?tab=files`);
        await page.getByRole("button", { name: fileRow("notes.md") }).click();
        const editor = page.getByRole("textbox", { name: "File content" });
        await expect(editor).toContainText("Original draft.");
        if (width === 390) {
          await expect(page.getByRole("button", { name: "Show tree", exact: true })).toBeVisible();
          await expect(page.getByRole("region", { name: "Selected file", exact: true })).toBeFocused();
        } else await expect(page.getByRole("button", { name: "Hide tree", exact: true })).toBeVisible();
        await editor.fill("# My draft\n\nKeep this.");
        if (width === 390) await page.getByRole("button", { name: "Show tree", exact: true }).click();
        await page.getByRole("button", { name: fileRow("app.py") }).click();
        const confirmation = page.getByRole("dialog", { name: "Discard unsaved edits?" });
        await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
        await expect(editor).toContainText("Keep this.");
        await expect(page).toHaveURL(/file=notes.md$/);
        await expect(page.getByRole("button", { name: "Hide tree", exact: true })).toBeVisible();
        await page.getByRole("button", { name: fileRow("app.py") }).click();
        await confirmation.getByRole("button", { name: "Discard draft", exact: true }).click();
        await expect(editor).toContainText("hello");
        await expect(page.getByRole("button", { name: width === 390 ? "Show tree" : "Hide tree", exact: true })).toBeVisible();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "files-selected-phone.png" : "files-selected.png") });
      });

      test(`workspace Files uses a readable title and truthful breadcrumb in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.route("**/api/files/workspace/demo-session", r => r.fulfill({ json: { entries: [], working: false } }));
        await page.goto("/workspaces/market-scan-20261025/files?session=demo-session");
        await expect(page.getByRole("heading", { name: "Market scan", exact: true })).toBeVisible();
        const crumbs = page.getByRole("navigation", { name: "Breadcrumb", exact: true });
        await expect(crumbs).toContainText("Market scan");
        await expect(crumbs).toContainText("Files");
        await expect(crumbs).not.toContainText("Wiki");
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "workspace-files-phone.png" : "workspace-files.png") });
      });

      test(`project section deep links select and focus their tab in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const project = await gitProject(page, `sections-${width}-${theme}`);
        await gotoLoadedPage(page, `/projects/${project.id}#project-ports`, ["ProjectPage"], ["/api/projects"]);
        const tabs = page.getByRole("tablist", { name: "Project sections", exact: true });
        await expect(tabs.getByRole("tab", { name: "Ports", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(tabs.getByRole("tab", { name: "Ports", exact: true })).toBeFocused();
        await expect(page.locator("#project-ports")).toBeVisible();
        await tabs.getByRole("tab", { name: "Knowledge", exact: true }).click();
        await expect(page.locator("#project-knowledge")).toBeVisible();
        await expect(page.locator("#project-ports")).toBeHidden();
        await expect(page).toHaveURL(/#project-knowledge$/);
        await expectAxeClean(page);
        if (theme === "dark") {
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.screenshot({ animations: "disabled", fullPage: true,
            path: wikiShotPath(testInfo, width === 390 ? "project-sections-phone.png" : "project-page.png") });
        }
      });

      test(`Security drafts stay unsaved through review and acknowledge explicit apply in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const project = await gitProject(page, `security-draft-${width}-${theme}`);
        await gotoLoadedPage(page, `/settings?security_scope=project&project_id=${project.id}#security`, ["Settings"], ["/api/projects", "/api/security/scopes", "/api/security/effective"]);
        const card = page.locator('#security');
        const rule = card.getByRole('switch', { name: 'Block force pushes', exact: true });
        await expect(rule).toBeChecked();
        const checked = await rule.isChecked();
        await rule.setChecked(!checked);
        await expect(card.getByText('Unsaved Security changes', { exact: false })).toBeVisible();
        expect((await (await page.request.get(`/api/security/effective?project_id=${project.id}`)).json()).switches.force_push).toBe(checked);
        await expectAxeClean(page);
        if (theme === 'dark') await card.screenshot({ animations: 'disabled',
          path: wikiShotPath(testInfo, width === 390 ? 'security-draft-phone.png' : 'security-draft.png') });
        const review = await reviewDraft(page);
        // Review is still a draft: only explicit apply changes the server policy.
        expect((await (await page.request.get(`/api/security/effective?project_id=${project.id}`)).json()).switches.force_push).toBe(checked);
        const applying = page.waitForResponse(response => response.request().method() === 'POST'
          && /\/api\/security\/changes\/[^/]+\/apply$/.test(new URL(response.url()).pathname));
        await review.getByRole('button', { name: 'Apply Security changes', exact: true }).click();
        expect((await applying).ok()).toBe(true);
        expect(await (await applying).finished()).toBeNull();
        await expect(review).toBeHidden();
        await expect(card.getByRole('status')).toContainText('Server policy saved');
        await expect(card.getByRole('region', { name: 'Effective Security status', exact: true })).toContainText('Custom');
        expect((await (await page.request.get(`/api/security/effective?project_id=${project.id}`)).json()).switches.force_push).toBe(!checked);
      });
    }
  });
}
