import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test(`bad issuer stays a readable keyboard-repairable error at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/settings#company-sign-in");
    const card = page.locator("#company-sign-in");
    const issuer = card.getByRole("textbox", { name: "Issuer", exact: true });
    for (const bad of ["https://[", "https://idp.example.com:bad"]) {
      await issuer.fill(bad);
      const response = page.waitForResponse(r => r.url().endsWith("/api/organizations/oidc") && r.request().method() === "PUT");
      await card.getByRole("button", { name: "Save", exact: true }).focus();
      await page.keyboard.press("Enter");
      expect((await response).status()).toBe(422);
      await expect(card.getByRole("alert").filter({ hasText: "Enter the issuer as an https address, without a query, fragment or user name." })).toBeVisible();
      await expect(issuer).toHaveValue(bad);
    }
    await issuer.focus();
    await page.keyboard.press("ControlOrMeta+A");
    await page.keyboard.type("https://idp.example.com/tenant");
    await expect(issuer).toHaveValue("https://idp.example.com/tenant");
    await expectAxeClean(page, "#company-sign-in");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("an old machine dialog cannot arm downtime for a replacement enrollment", async ({ page }) => {
  const name = `b6-${randomUUID().slice(0, 8)}`;
  await page.goto("/host");
  const headers = await csrfHeaders(page);
  const create = () => page.request.post("/api/nodes", { headers, data: { name, role: "always_on" } });
  try {
    expect((await create()).status()).toBe(201);
    await page.reload();
    await page.getByRole("button", { name: `Machine settings for ${name}`, exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.locator('[name="downtime-until-reconnect"]').check();
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).status()).toBe(200);
    expect((await create()).status()).toBe(201);
    const response = page.waitForResponse(r => r.url().endsWith(`/api/nodes/${name}/policy`));
    await dialog.getByRole("button", { name: "Save", exact: true }).click();
    expect((await response).status()).toBe(409);
    await expect(dialog.getByText(/Machine settings changed/)).toBeVisible();
    const current = (await (await page.request.get("/api/nodes")).json()).find((n: { name: string }) => n.name === name);
    expect(current.downtime_until_reconnect).toBe(false);
  } finally {
    await page.request.post(`/api/nodes/${name}/remove`, { headers });
  }
});
