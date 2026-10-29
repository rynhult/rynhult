import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

// First-run review follow-ups (docs/handoffs/2026-10-27-1235e44c1732-first-run-rc.md): plain words a new owner can act on.
test.use({ storageState: AUTH_FILE });

test("passkeys mention company sign-in only where the sign-in page offers it", async ({ page }) => {
  let sso = false;
  await page.route("**/api/auth/state", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), sso } });
  });
  await page.route("**/api/auth/passkeys", route => route.fulfill({ json: {
    passkeys: [], recovery_code: false, policy: { owners: "allow", members: "allow" },
    company_only: false, enrollment_until: null,
  } }));
  await page.goto("/settings#passkeys");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Passkeys", exact: true }) }).first();
  await expect(card.getByRole("heading", { name: "Owner policy", exact: true })).toBeVisible();
  await expect(card).toContainText("instead of your password and authenticator code");
  await expect(card).toContainText("Add a second one (your phone or a security key)");
  await expect(card).not.toContainText("company");
  await expect(card).toContainText("Anyone with a Rynhult password also needs a recovery code.");
  sso = true;
  await page.reload();
  await expect(card.getByRole("heading", { name: "Owner policy", exact: true })).toBeVisible();
  await expect(card).toContainText("People who sign in with your company account recover through company sign-in instead.");
  await expectAxeClean(page, "main");
});

test("New session explains profiles before one is chosen, then shows the chosen one's description", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /^New session$/ }).first().click();
  const dialog = page.getByRole("dialog");
  const hint = dialog.getByText(/^Optional: a profile gives the agent a role/);
  await expect(hint).toBeVisible();
  await expect(hint).toContainText("Not sure? Leave None, or choose Full-stack to build an app end to end.");
  await dialog.getByLabel("Profile").selectOption({ label: "Full-stack" });
  await expect(hint).toHaveCount(0);
  await expect(dialog.getByText("End-to-end features across UI, API and data.")).toBeVisible();
  await page.keyboard.press("Escape");
});

test("backup target setup explains each field in plain words", async ({ page }) => {
  await page.goto("/host/backups/setup");
  await expect(page.getByLabel("Target type")).toHaveAccessibleDescription(/Where the backups go/);
  await expect(page.getByRole("option", { name: "Another Linux machine (receiver)" })).toBeAttached();
  await expect(page.getByRole("option", { name: "Cloud storage (S3-compatible)" })).toBeAttached();
  await expect(page.getByLabel("Machine to keep the backups on")).toHaveAccessibleDescription(/can add backups but never delete them/);
  await expect(page.getByLabel("Maximum backup space (GiB)")).toHaveAccessibleDescription(/half its free space/);
  await expect(page.getByText(/A folder on this server is set up by an administrator instead/)).toBeVisible();
  await expect(page.getByText("Repository ceiling", { exact: false })).toHaveCount(0);
  await expectAxeClean(page, "main");
});

test("Providers says which machine and agent account its sign-ins are for, as a sentence", async ({ page }) => {
  await page.goto("/providers");
  await expect(page.locator("#provider-sign-ins")).toContainText(/Sign-ins: on acc-host \(the main Rynhult\), for sessions running as \S+\./);
});
