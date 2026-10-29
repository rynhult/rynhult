import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, gitProject, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

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
        await page.goto(`/projects/${project.id}#project-ports`);
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

      test(`guardrail drafts explain unsaved state and acknowledge an explicit save in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.goto("/settings#guardrails");
        const original = await (await page.request.get("/api/guardrails")).json();
        const card = page.locator("#guardrails");
        const rule = card.getByRole("checkbox").first();
        const checked = await rule.isChecked();
        try {
          await rule.setChecked(!checked);
          await expect(card.getByRole("status", { name: "Guardrail save status" })).toContainText("Unsaved changes");
          expect((await (await page.request.get("/api/guardrails")).json()).rules).toEqual(original.rules);
          await expectAxeClean(page);
          if (theme === "dark") await card.screenshot({ animations: "disabled",
            path: wikiShotPath(testInfo, width === 390 ? "guardrails-draft-phone.png" : "guardrails-draft.png") });
          await card.getByRole("button", { name: "Save guardrails", exact: true }).click();
          await expect(card.getByRole("status", { name: "Guardrail save status" })).toContainText("Guardrails saved");
          expect((await (await page.request.get("/api/guardrails")).json()).rules[original.available[0].id]).toBe(!checked);
        } finally {
          const restored = await page.request.put("/api/guardrails", { headers: await csrfHeaders(page),
            data: { rules: original.rules, custom: original.custom } });
          expect(restored.ok()).toBe(true);
        }
      });
    }
  });
}
