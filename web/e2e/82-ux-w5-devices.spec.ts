import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

// UX declutter wave 5 (D05): access devices and notification devices stay separate registries, each pointing to the
// other with its consequence; the app-install steps are one disclosure away, the privacy note stays visible.
test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });

    test("access and notification devices link to each other and keep their own actions", async ({ page }) => {
      await page.goto("/settings#all");
      const access = page.locator("#access-devices");
      const alerts = page.locator("#notification-devices");
      await expect(access.getByRole("heading", { name: "Access devices", exact: true })).toBeVisible();
      await expect(alerts.getByRole("heading", { name: "Notification devices", exact: true })).toBeVisible();
      await expect(access.getByRole("button", { name: "Pair device", exact: true })).toBeVisible();
      await expect(alerts.getByRole("button", { name: "Pair device" })).toHaveCount(0);

      await access.getByRole("link", { name: "Notification devices", exact: true }).click();
      await expect(page).toHaveURL(/#notification-devices$/);
      await expect(alerts).toBeFocused();
      await alerts.getByRole("link", { name: "Access devices", exact: true }).click();
      await expect(page).toHaveURL(/#access-devices$/);
      await expect(access).toBeFocused();

      await access.getByRole("link", { name: "Notification devices", exact: true }).click();
      const install = alerts.getByText("Install Rynhult as an app", { exact: true });
      const steps = alerts.getByText(/Safari → Share → Add to Home Screen/);
      await expect(steps).toBeHidden();
      await expect(alerts.getByText(/Messages are encrypted for each device/)).toBeVisible();
      await install.focus();
      await page.keyboard.press("Enter");
      await expect(steps).toBeVisible();
      await expectAxeClean(page, "main");
    });
  });
}
