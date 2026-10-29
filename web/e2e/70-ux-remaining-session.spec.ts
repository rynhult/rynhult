import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

async function sessionFixture(page: Page) {
  const session = { ...demoSessions()[5], id: "bbbbbbbbbb91", project_id: 91, project: "demo-api",
    name: "Fix login", repo: "example-org/demo-api", changes: { git: true, branch: "fix/login" },
    worktree: "/srv/acc-demo/.worktrees/login", worktree_branch: "fix/login",
    launch_spec: { inputs: {}, files: {}, digest: "demo" }, runs: [], editor: null };
  await page.route("**/api/sessions", r => r.fulfill({ json: [session] }));
  await page.route(`**/api/sessions/${session.id}`, r => r.fulfill({ json: session }));
  return session;
}

for (const width of [1440, 390]) {
  test.describe(`${width}px session decisions`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`pull readiness leads with blockers and links to checks and reviews in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = await sessionFixture(page);
        await page.route(`**/api/sessions/${session.id}/pull`, r => r.fulfill({ json: {
          provider: "github", repo: "example-org/demo-api", repo_url: "https://github.com/example-org/demo-api",
          branch: "fix/login", default_branch: "main", pushed: true, signed_in: "gh",
          pull: { number: 7, title: "Fix login", body: "A long description.\n".repeat(50),
            url: "https://github.com/example-org/demo-api/pull/7", state: "open", draft: false, base: "main",
            head_sha: "a".repeat(40), mergeable: true, mergeable_state: "blocked", reviewers: ["riley-example"], author: "alex-example" },
          checks: [
            { name: "test", kind: "check", status: "completed", conclusion: "failure", url: null, required: true },
            { name: "lint", kind: "check", status: "in_progress", conclusion: null, url: null, required: true },
          ], threads: 2,
          merge: { methods: ["squash"], blockers: ["Required check 'test' failure.", "2 unresolved review threads."],
            notes: [], required: ["test", "lint"], allowed: false },
        } }));
        await page.goto(`/sessions/${session.id}?tab=pull`);
        const readiness = page.getByRole("region", { name: "Pull request readiness" });
        await expect(readiness).toBeVisible({ timeout: 20000 });
        await expect(readiness).toContainText("Required check 'test' failure.");
        await expect(readiness).toContainText("1 failed");
        await expect(readiness).toContainText("1 running");
        await expect(page.getByRole("button", { name: "Squash and merge", exact: true })).toBeDisabled();
        const summaryBox = await readiness.boundingBox();
        const descriptionBox = await page.getByRole("region", { name: "Pull request description" }).boundingBox();
        expect(summaryBox!.y).toBeLessThan(descriptionBox!.y);
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "sessions-pull-readiness-phone.png" : "sessions-pull-readiness.png") });
        await readiness.getByRole("link", { name: /Review comments/ }).click();
        await expect(page.locator("#pull-reviews")).toBeFocused();
        await expect(page).toHaveURL(/tab=pull#pull-reviews$/);
      });

      test(`preview identifies the project and session without claiming an exclusive port in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = await sessionFixture(page);
        const { port } = await (await page.request.get('/preview-test-port')).json();
        const secondPort = port + 1;
        const ingress = new URL(testInfo.project.use.baseURL!);
        // UI-only fixture: a complete mint response and isolated, intercepted frame; never navigate to ACC or a peer run.
        await page.route(/^http:\/\/preview-91-\d+\.preview\.localhost:\d+\//, r => r.fulfill({
          contentType: 'text/html', body: '<!doctype html><html lang="en"><title>Preview fixture</title><body>Fixture preview</body></html>',
        }));
        await page.route("**/api/projects/91/preview", r => {
          const selected = r.request().method() === 'POST' ? r.request().postDataJSON().port : port;
          const origin = `http://preview-91-${selected}.preview.localhost:${ingress.port}`;
          return r.fulfill({ json: r.request().method() === "POST"
            ? { url: `${origin}/`, frame_url: `${origin}/frame`, expires_in: 300 } : { available: true, ports: [port, secondPort] } });
        });
        await page.goto(`/sessions/${session.id}?tab=preview`);
        const context = page.getByRole("region", { name: "Preview context" });
        await expect(context).toBeVisible({ timeout: 20000 });
        await expect(context).toContainText("demo-api");
        await expect(context).toContainText("fix/login");
        await expect(context).toContainText("/srv/acc-demo/.worktrees/login");
        await expect(context).toContainText("shared");
        await expect(page.getByRole("link", { name: "Open in new tab", exact: true })).toHaveAttribute("target", "_blank");
        await page.getByRole("combobox", { name: "Preview port", exact: true }).selectOption(String(secondPort));
        await expect(page).toHaveURL(new RegExp(`port=${secondPort}`));
        await expect(page.frameLocator('iframe[title="Project preview"]').getByText('Fixture preview', { exact: true })).toBeVisible();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "preview-context-phone.png" : "preview-context.png") });
      });
    }
  });
}
