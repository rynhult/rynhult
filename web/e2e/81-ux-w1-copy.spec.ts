import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, gitProject } from "./helpers";

// UX declutter wave 1 (copy and naming): access and notification devices are named apart, old links still land, and
// New session's name sits after Agent/Profile with short routine hints.
test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });

    test("Settings names access and notification devices apart and keeps the old links", async ({ page }) => {
      await page.goto("/settings#all");
      const index = page.getByRole("navigation", { name: "Settings sections" });
      await expect(index.getByRole("link", { name: "Access devices", exact: true })).toBeVisible();
      await expect(index.getByRole("link", { name: "Notification devices", exact: true })).toBeVisible();
      await expect(index.getByRole("link", { name: "Your devices", exact: true })).toHaveCount(0);
      await expect(page.locator("#notification-devices")).toContainText("removing one does not revoke access");
      for (const [old, now] of [["your-devices", "access-devices"], ["devices", "notification-devices"]]) {
        await page.goto(`/settings#${old}`);
        await expect(page.locator(`#${now}`)).toBeFocused();
      }
      await page.goto("/settings#access-devices");
      await expect(page.getByRole("heading", { name: "Access devices", exact: true })).toBeVisible();
      await expectAxeClean(page, "main");
    });

    test("New session keeps the task first and names the session after Agent and Profile", async ({ page }) => {
      const project = await gitProject(page, `w1-${width}`);
      await page.goto("/");
      await page.getByRole("button", { name: "New session", exact: true }).first().click();
      const dialog = page.getByRole("dialog", { name: "New session" });
      await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
      await expect(dialog.getByText("Sent once when the session starts. Without one, the agent waits for you.")).toBeVisible();
      const field = (role: "combobox" | "textbox", name: string) => dialog.getByRole(role, { name, exact: true });
      const top = async (role: "combobox" | "textbox", name: string) => (await field(role, name).boundingBox())!.y;
      expect(await top("textbox", "Task (optional)")).toBeLessThan(await top("combobox", "Agent"));
      const rename = dialog.getByRole("button", { name: "Rename", exact: true });
      expect(await top("combobox", "Profile")).toBeLessThan((await rename.boundingBox())!.y);  // W6: the name is a Rename row
      await rename.click();
      await field("textbox", "Name").fill("renamed-session");
      await expectAxeClean(page, '[role="dialog"]');
    });
  });
}
