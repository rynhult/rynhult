import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, gotoAppPage, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

async function sessionFixture(page: Page) {
  const session = { ...demoSessions()[5], name: "Fix checkout", host: "local",
    launch_spec: { inputs: {}, files: {}, digest: "demo" }, runs: [], editor: null };
  await page.route("**/api/sessions", route => route.fulfill({ json: [session] }));
  await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({ json: session }));
  return session;
}

for (const width of [1440, 390]) {
  test.describe(`${width}px session outputs`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`deliverables count only this session's notes and link to its artifacts in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = await sessionFixture(page);
        const doc = (id: string) => ({ path: `docs/handoffs/2026-10-25-${id}-checkout.md`, bytes: 80,
          modified: Date.now() / 1000, commit: null, committed_at: null, subject: null });
        await page.route(`**/api/sessions/${session.id}/knowledge`, route => route.fulfill({
          json: { repo: null, folder: "worktree", docs: { handoffs: [doc(session.id), doc("another-session")] } },
        }));
        let artifactNames = ["test-log.txt", "review.txt"];
        await page.route(`**/api/sessions/${session.id}/artifacts`, route => route.fulfill({
          json: { truncated: false, files: artifactNames.map(name =>
            ({ name, kind: "text", size: 120, mtime: 1, too_large: false })) },
        }));
        await gotoAppPage(page, `/sessions/${session.id}?tab=handoffs`);
        if (width === 390) await page.getByRole("button", { name: "Session details" }).click();
        const outputs = page.getByRole("group", { name: "Session deliverables" });
        await expect(outputs.getByRole("link", { name: "1 handoff", exact: true })).toBeVisible();
        await expect(outputs.getByRole("link", { name: "2 artifacts", exact: true })).toBeVisible();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, `sessions-deliverables${width === 390 ? "-phone" : ""}.png`) });
        await outputs.getByRole("link", { name: "2 artifacts", exact: true }).click();
        await expect(page).toHaveURL(/tab=artifacts$/);
        await expect(page.getByText("test-log.txt", { exact: true })).toBeVisible();
        await expect(page.getByText("review.txt", { exact: true })).toBeVisible();
        if (width === 390) await expect(outputs).not.toBeVisible();
        artifactNames = [];
        const emptyArtifacts = page.waitForResponse(response => new URL(response.url()).pathname === `/api/sessions/${session.id}/artifacts` && response.request().method() === "GET");
        await page.reload();
        expect((await emptyArtifacts).ok()).toBe(true);
        await expect(page.getByText("No artifacts yet. Screenshots, short videos, test logs and reports appear here when the agent leaves them for you.")).toBeVisible();
        const convention = page.getByText(".acc/artifacts/", { exact: true });
        await expect(convention).not.toBeVisible();
        await page.getByText("Where do artifacts come from?", { exact: true }).click();
        await expect(convention).toBeVisible();
        await expectAxeClean(page);
      });

      test(`failed output reads never claim zero handoffs or artifacts in ${theme}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = await sessionFixture(page);
        for (const endpoint of ["knowledge", "artifacts"]) {
          await page.route(`**/api/sessions/${session.id}/${endpoint}`, route => route.fulfill({
            status: 503, json: { detail: "Temporarily unavailable" },
          }));
        }
        await gotoAppPage(page, `/sessions/${session.id}?tab=handoffs`);
        if (width === 390) await page.getByRole("button", { name: "Session details" }).click();
        const outputs = page.getByRole("group", { name: "Session deliverables" });
        await expect(outputs.getByRole("link", { name: "Handoffs unavailable", exact: true })).toBeVisible();
        await expect(outputs.getByRole("link", { name: "Artifacts unavailable", exact: true })).toBeVisible();
        await expect(outputs).not.toContainText("0 handoffs");
        await expect(outputs).not.toContainText("No artifacts yet");
        if (width === 390) {
          // The open Details popover covers the page below it, so check it alone, then the page once it is closed: with
          // 44 px tap targets on touch it overlaps the failed read's own Details toggle.
          await expectAxeClean(page, '[role="group"][aria-label="Session details"]');
          await page.keyboard.press("Escape");
          await expect(outputs).toBeHidden();
        }
        await expectAxeClean(page);
      });

      test(`Changes explains the current branch comparison rather than a stale launch base in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = await sessionFixture(page);
        let comparisonRef = "main";
        await page.route(`**/api/sessions/${session.id}/changes`, route => route.fulfill({
          json: { git: true, revision: "demo", base: "abcdef123456", comparison_ref: comparisonRef, since_launch: false,
            branch: "fix-checkout", files: 1, additions: 1, deletions: 0, truncated: false,
            list: [{ path: "app.py", status: "M", additions: 1, deletions: 0, binary: false }],
            diff: "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n one\n+two\n" },
        }));
        await gotoAppPage(page, `/sessions/${session.id}?tab=changes`);
        await expect(page.getByText(/current branch merge-base with main/)).toBeVisible();
        await expect(page.getByText(/session started before Rynhult/)).not.toBeVisible();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, `session-changes-baseline${width === 390 ? "-phone" : ""}.png`) });
        if (width === 390) {
          comparisonRef = `upstream/${"long-reference-".repeat(15)}`;
          const changedBaseline = page.waitForResponse(response => new URL(response.url()).pathname === `/api/sessions/${session.id}/changes` && response.request().method() === "GET");
          await page.reload();
          expect((await changedBaseline).ok()).toBe(true);
          await expect(page.getByText(/current branch merge-base with/)).toContainText(comparisonRef);
          expect(await page.getByRole("main").evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
          expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
        }
      });
    }
  });
}
