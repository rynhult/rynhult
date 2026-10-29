import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test(`withdraw only the selected duplicate company sign-in invite at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const username = `invite-${randomUUID().slice(0, 8)}`;
    const accounts = [`${username}-one`, `${username}-two`];
    const ids: number[] = [];
    const headers = await csrfHeaders(page);
    try {
      for (const account of accounts) {
        const response = await page.request.post("/api/organizations/sso-invites", {
          headers, data: { username, account, email: `${username}@example.com` },
        });
        expect(response.status()).toBe(201);
        const made: { id: number } = await response.json();
        ids.push(made.id);
      }
      await page.goto("/settings#company-sign-in");
      const card = page.locator("#company-sign-in");
      const rows = accounts.map((account) => card.getByRole("listitem").filter({ hasText: account }));
      const response = await page.request.get("/api/organizations/identities");
      const data: { invites: { id: number; created_at: string }[] } = await response.json();
      for (let i = 0; i < rows.length; i++) {
        await expect(rows[i].getByText(username, { exact: true })).toBeVisible();
        await expect(rows[i].getByText(`${username}@example.com`, { exact: true })).toBeVisible();
        await expect(rows[i].getByText(accounts[i], { exact: true })).toBeVisible();
        await expect(rows[i].locator("time")).toHaveAttribute("datetime", data.invites.find((invite) => invite.id === ids[i])!.created_at);
        await expect(rows[i].locator("time")).not.toBeEmpty();
      }
      await rows[0].getByRole("button", { name: "Withdraw", exact: true }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toContainText(accounts[0]);
      await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(rows[0]).toBeVisible();

      await page.route(`**/api/organizations/sso-invites/${ids[0]}`, (route) => route.fulfill({
        status: 409, json: { detail: "The sign-in link changed. Refresh and try again." },
      }), { times: 1 });
      await rows[0].getByRole("button", { name: "Withdraw", exact: true }).click();
      await dialog.getByRole("button", { name: "Withdraw", exact: true }).click();
      await expect(card.getByRole("alert")).toContainText("The sign-in link changed. Refresh and try again.");
      await expect(rows[0]).toBeVisible();

      await rows[0].getByRole("button", { name: "Withdraw", exact: true }).click();
      await dialog.getByRole("button", { name: "Withdraw", exact: true }).click();
      await expect(rows[0]).toHaveCount(0);
      await expect(rows[1]).toBeVisible();
      await page.reload();
      await expect(rows[0]).toHaveCount(0);
      await expect(rows[1]).toBeVisible();
      await expectAxeClean(page, "#company-sign-in");
      expect(await card.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
      await rows[1].screenshot({ path: testInfo.outputPath(`invite-withdraw-${width}.png`) });
    } finally {
      for (const id of ids) {
        const response = await page.request.delete(`/api/organizations/sso-invites/${id}`, { headers });
        expect([200, 404]).toContain(response.status());
      }
    }
  });

  test(`owner can read company failure diagnostics without a tooltip at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.route("**/api/audit?**", (route) => route.fulfill({ json: { has_more: false, next_before: null, items: [
      { id: 2, ts: "2026-10-25T19:00:00Z", actor: "-", action: "auth.sso_failed", target: "192.0.2.10",
        detail: { reason: "token_refused", issuer: "https://login.example.com/tenant", detail: "HTTP 401 from the token endpoint",
          oauth_error: "invalid_client", aadsts_code: "AADSTS7000215", error_code: "AADSTS7000215",
          error_text: "AADSTS7000215: Invalid client secret." } },
      { id: 1, ts: "2026-10-25T18:59:00Z", actor: "-", action: "auth.sso_failed", target: "192.0.2.10",
        detail: { reason: "invite_mismatch", issuer: "https://login.example.com/tenant", subject: "mallory-sub",
          invite: 10, invited_account: "dana@example.com", returned_account: "mallory@example.com" } },
    ] } }));
    await page.goto("/audit");
    await expect(page.getByText("Provider code: AADSTS7000215", { exact: true })).toBeVisible();
    await expect(page.getByText("AADSTS7000215: Invalid client secret.", { exact: true })).toBeVisible();
    await expect(page.getByText("Signed in as mallory@example.com; invitation is for dana@example.com.", { exact: true })).toBeVisible();
    await expectAxeClean(page);
    await page.screenshot({ path: testInfo.outputPath(`sso-failures-${width}.png`) });
  });
}

test("removing a linked member's company sign-in says they keep their own sign-in", async ({ page }) => {
  // A linked identity needs a real provider round trip; the list is stubbed, and the DELETE is answered here.
  const linked = { id: 9001, username: "erin", person_id: "p-erin", issuer: "https://login.example.com/v2.0",
    subject: "erin-subject", created_at: "2026-10-26T08:00:00Z", last_login_at: null, removes_person: false };
  await page.route("**/api/organizations/identities", (route) => route.fulfill({ json: { identities: [linked], invites: [] } }));
  let deleted = "";
  await page.route("**/api/organizations/identities/9001*", (route) => {
    deleted = route.request().url();
    return route.fulfill({ json: { ok: true, removed: false } });
  });
  await page.goto("/settings#company-sign-in");
  await page.locator("#company-sign-in").getByRole("button", { name: "Stop erin signing in with the provider" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("erin also has a password and authenticator of their own, so they stay a member");
  await expect(dialog.getByRole("radio")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Stop company sign-in" }).click();
  await expect.poll(() => deleted).toMatch(/\/identities\/9001$/);
});

test.describe("anonymous failures", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a withdrawn invitation has a clear public failure", async ({ page }) => {
    await page.goto("/login?sso=invite_withdrawn");
    await expect(page.getByRole("alert")).toContainText("withdrawn");
    await expect(page.getByRole("alert")).toContainText("owner");
  });

  test("account mismatch and provider diagnostics are not disclosed on the public failure page", async ({ page }) => {
    await page.goto("/login?sso=invite_mismatch&returned_account=mallory@example.com&invited_account=dana@example.com&error_code=AADSTS7000215");
    await expect(page.getByRole("alert")).toContainText("different company account");
    await expect(page.locator("body")).not.toContainText("mallory@example.com");
    await expect(page.locator("body")).not.toContainText("dana@example.com");
    await expect(page.locator("body")).not.toContainText("AADSTS7000215");
  });
});
