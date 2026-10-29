import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, wikiShotPath } from "./helpers";

/* Host → Machines shows each machine's public identity read-only (Remote access → machine identities).
   The demo machine lab-box is given each state through a route; nothing is issued or signed here. */
test.use({ storageState: AUTH_FILE });

const days = (n: number) => new Date(Date.now() + n * 86400e3).toISOString();

async function machineState(page: Page, identity: object | null, connected = true) {
  await page.unroute("**/api/nodes");
  await page.route("**/api/nodes", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const rows = await (await route.fetch()).json();
    await route.fulfill({ json: rows.map((row: { name: string }) =>
      row.name === "lab-box" ? { ...row, connected, machine_identity: identity } : row) });
  });
  await page.goto("/host");
  return page.getByRole("region", { name: "Public identity of lab-box" });
}

for (const width of [1440, 390]) {
  test(`Machines shows each machine's public identity at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    // Identities off and never issued: nothing extra in the row.
    await expect(await machineState(page, { enabled: false, issued: false })).toHaveCount(0);

    let row = await machineState(page, { enabled: true, issued: false });
    await expect(row).toContainText("Public identity: being issued over its link.");
    row = await machineState(page, { enabled: true, issued: false }, false);
    await expect(row).toContainText("issued when it next connects");

    row = await machineState(page, { enabled: true, issued: true, fingerprint: "a".repeat(64), expires_at: days(25),
      rotating: false, last_presented_at: new Date(Date.now() - 2 * 3600e3).toISOString() });
    await expect(row).toContainText(/Public identity valid until .+ · last used through the public address 2h ago\./);
    await expect(row.getByRole("link", { name: "Remote access" })).toHaveAttribute("href", "/settings/remote-access");
    await expect(row.getByText(/overdue|Expires soon/)).toHaveCount(0);
    await expectAxeClean(page);
    if (width === 1440) await row.locator("xpath=..").screenshot({ path: wikiShotPath(testInfo, "machines-public-identity.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

    row = await machineState(page, { enabled: true, issued: true, fingerprint: "b".repeat(64), expires_at: days(8),
      rotating: true, last_presented_at: null });
    await expect(row).toContainText("new certificate issued, switching after its next connection");
    await expect(row).toContainText("not yet used through the public address");

    row = await machineState(page, { enabled: true, issued: true, fingerprint: "c".repeat(64), expires_at: days(2),
      rotating: false, last_presented_at: null }, false);
    await expect(row.getByText("Expires soon: reconnect this machine")).toBeVisible();
    row = await machineState(page, { enabled: true, issued: true, fingerprint: "c".repeat(64), expires_at: days(2),
      rotating: false, last_presented_at: null });
    await expect(row.getByText("Renewal overdue: check Remote access")).toBeVisible();
    await expectAxeClean(page);

    row = await machineState(page, { enabled: false, issued: true, fingerprint: "d".repeat(64), expires_at: days(20),
      rotating: false, last_presented_at: null });
    await expect(row).toContainText("Public identity off. Its last certificate is no longer admitted.");
  });
}

test("A device-authority rollover is shown on Remote access, Devices and Machines", async ({ page }) => {
  // Routes stand in for the signer's answer; no authority is staged or promoted here.
  await page.route("**/api/remote-access", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const state = await (await route.fetch()).json();
    await route.fulfill({ json: { ...state, rollover: { authority: "ab12".repeat(16), previous_identities: 2 } } });
  });
  await page.route((url) => url.pathname === "/api/devices", (route) => route.fulfill({ json: {
    pairing_enabled: true, policy_pending: false, devices: [{ id: "1".repeat(32), person: "casey", label: "Casey's phone",
      kind: "browser", status: "active", expires_at: days(60), last_seen_at: null, previous_authority: true }] } }));
  await page.goto("/settings/remote-access");
  await expect(page.getByText("2 identities still come from the previous device authority.", { exact: false })).toBeVisible();
  await expect(page.getByText("ab12ab12ab12ab12…")).toBeVisible();
  await expect(page.getByText(/Issued by the previous device authority: renew it on the LAN/)).toBeVisible();
  await expectAxeClean(page);
  const row = await machineState(page, { enabled: true, issued: true, fingerprint: "e".repeat(64), expires_at: days(20),
    rotating: false, last_presented_at: null, previous_authority: true });
  await expect(row).toContainText("from the previous device authority, replaced over its link");
});
