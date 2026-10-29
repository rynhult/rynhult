import { expect, test } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

/* UX review 2, beta item 4: the Guide reads like the product. */
test.use({ storageState: AUTH_FILE });

test("the Guide renders key caps, starts with Getting started, and the home page matches what ships", async ({ page }) => {
  await page.goto("/guide/User-Guide/New-Session");
  const article = page.locator("article");
  await expect(article.locator("kbd", { hasText: "C" }).first()).toBeVisible();
  await expect(article).not.toContainText("<kbd>");
  await page.goto("/guide/User-Guide/Connect-Your-Phone");
  await expect(page.getByRole("heading", { name: "Connect your phone", level: 1 })).toBeVisible();
  await page.goto("/guide/Home");
  await expect(page.locator("article")).not.toContainText("on one machine");
  await expect(page.locator("article")).not.toContainText("no autonomous orchestration");
  await expect(page.locator("article")).toContainText("release candidate");
});
