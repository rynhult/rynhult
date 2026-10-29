import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Quick wins QW-E (NAV-5, owner decision 0008 Q7) and part of QW-C (SL-2 with Q4, OV-3).
test("theme follows the system by default, and the button cycles System, Light, Dark", async ({ browser }) => {
  const context = await browser.newContext({ storageState: AUTH_FILE, colorScheme: "light", viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("acc-theme"));
  await page.reload();
  const html = page.locator("html");
  await expect(html).not.toHaveClass(/dark/);
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(html).toHaveClass(/dark/);  // follows a change while open
  await page.getByRole("button", { name: /^Theme: System/ }).click();
  await expect(html).not.toHaveClass(/dark/);
  expect(await page.evaluate(() => localStorage.getItem("acc-theme"))).toBe("light");
  await page.getByRole("button", { name: /^Theme: Light/ }).click();
  await expect(html).toHaveClass(/dark/);
  await page.getByRole("button", { name: /^Theme: Dark/ }).click();
  expect(await page.evaluate(() => localStorage.getItem("acc-theme"))).toBeNull();
  await expect(page.getByRole("button", { name: /^Theme: System/ })).toBeVisible();
  await context.close();
});

test("a guardrail refusal shows an amber Blocked prefix with its age; the CPU stat says where it came from", async ({ page }) => {
  const capturedNow = new Date();
  await page.clock.setFixedTime(capturedNow);
  const sessions = demoSessions().map((s) => s.name === "checkout-flow"
    ? { ...s, activity: "Blocked: Running git push --force origin main", activity_at: new Date(capturedNow.getTime() - 4 * 60_000).toISOString() } : s);
  await page.route("**/api/sessions", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(sessions) }));
  await page.goto("/sessions");
  const row = page.getByRole("row").filter({ hasText: "checkout-flow" });
  const prefix = row.locator(".text-wait", { hasText: "Blocked 4m ago:" });
  await expect(prefix).toBeVisible();
  await expect(row).toContainText("Running git push --force origin main");
  await expect(row.getByText("Working")).toBeVisible();  // still a history note, not a new state (Q4)
  await page.goto("/");
  await expect(page.getByText(/live \(psutil\)/)).toBeVisible();
});
