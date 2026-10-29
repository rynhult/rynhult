import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px remaining admin flows`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`publishing selects a project's reserved port and reviews the audience in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.route("**/api/projects", r => r.fulfill({ json: [
          { id: 91, name: "Demo API", slug: "demo-api", import_mode: "managed", archived: false },
          { id: 92, name: "Demo Web", slug: "demo-web", import_mode: "managed", archived: false },
        ] }));
        await page.route("**/api/ports", r => r.fulfill({ json: [
          { port: 8004, project_id: 91, project: "demo-api", range: "dev", mine: true },
          { port: 8102, project_id: 92, project: "demo-web", range: "prod", mine: true },
        ] }));
        await page.route("**/api/publish/zones", r => r.fulfill({ json: {
          configured: true, zones: [{ id: "example", name: "example.com" }], public_ip: "192.0.2.1", internal_ip: "10.0.0.1",
        } }));
        let published = false;
        let submitted: Record<string, unknown> | undefined;
        await page.route(/\/api\/publish(?:\?.*)?$/, r => r.request().method() === "POST"
          ? (published = true, submitted = r.request().postDataJSON(), r.fulfill({ json: {} })) : r.fulfill({ json: [] }));
        await page.goto("/publishing");
        await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("91");
        await page.getByRole("combobox", { name: "Reserved port", exact: true }).selectOption("8004");
        await expect(page.getByRole("combobox", { name: "Reserved port" }).getByRole("option", { name: /8102/ })).toHaveCount(0);
        await page.getByLabel("Subdomain").fill("demo");
        await page.getByRole("combobox", { name: "Access", exact: true }).selectOption("public");
        await expect(page.getByRole("status", { name: "Publication preview" })).toContainText("https://demo.example.com");
        await expect(page.getByRole("status", { name: "Publication preview" })).toContainText("Public internet");
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "publishing-project-phone.png" : "publishing.png") });
        await page.getByRole("button", { name: "Publish", exact: true }).click();
        const confirmation = page.getByRole("dialog", { name: "Publish demo.example.com publicly?" });
        await expect(confirmation).toContainText("8004");
        await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
        expect(published).toBe(false);
        await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("92");
        await expect(page.getByRole("combobox", { name: "Reserved port" })).toHaveValue("");
        await expect(page.getByRole("button", { name: "Publish", exact: true })).toBeDisabled();
        await page.getByRole("combobox", { name: "Reserved port" }).selectOption("8102");
        await page.getByRole("combobox", { name: "Access", exact: true }).selectOption("internal");
        await page.getByRole("button", { name: "Publish", exact: true }).click();
        await expect.poll(() => submitted).toMatchObject({ host: "demo.example.com", port: 8102, access: "internal", replace: false });
      });

      test(`Providers sections retain machine scope and are deep-linkable in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.goto("/providers#provider-tools");
        const tools = page.locator("#provider-tools");
        await expect(tools).toBeFocused();
        await expect(tools).toContainText("acc-host");
        const navigation = page.getByRole("group", { name: "Provider sections" });
        await navigation.getByRole("link", { name: "Sign-ins and usage", exact: true }).click();
        await expect(page).toHaveURL(/#provider-sign-ins$/);
        await expect(page.locator("#provider-sign-ins")).toBeFocused();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "providers-sections-phone.png" : "providers.png") });
      });
    }
  });
}
