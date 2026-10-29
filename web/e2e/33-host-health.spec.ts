import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

/* UX review 2, beta item 5: Host shows this computer's health first, then backups as one line, then other machines. */
test.use({ storageState: AUTH_FILE });

test("Host leads with health on desktop and phone; machines don't shout", async ({ page }) => {
  await page.route("**/api/nodes", (route) => route.fulfill({ json: [{ id: 1, name: "build-box", status: "active", connected: true,
    last_seen: new Date().toISOString(), accounts: ["alex-agent"], info: { hostname: "build-box", os: "linux", arch: "x86_64" },
    update_available: true, software: {}, logins: { "alex-agent": { claude: true, copilot: false, checked_at: 1 } } }] }));
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.goto("/host");
    const cpu = page.getByText("CPU", { exact: true }).first();
    const backups = page.getByRole("heading", { name: "Backups", exact: true });
    const machines = page.getByRole("heading", { name: "Machines", exact: true });
    await expect(machines).toBeVisible();
    const [c, b, m] = await Promise.all([cpu, backups, machines].map(async (x) => (await x.boundingBox())!.y));
    expect(c).toBeLessThan(b);  // health, then backups, then machines
    expect(b).toBeLessThan(m);
    expect(c).toBeLessThan(viewport.height);  // on the first screen
  }
  await expect(page.getByText("Update available")).toHaveCount(0);  // no version reported: nothing to compare
  await expect(page.getByText("Copilot not signed in")).toBeVisible();  // quiet, not a red badge
  await expect(page.getByText("signed out")).toHaveCount(0);
  await expectAxeClean(page);
});

for (const width of [1440, 390]) for (const theme of ["dark", "light"]) {
  test(`Host names media errors outside healthy details, ${width}px ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.addInitScript(t => localStorage.setItem("acc-theme", t), theme);
    await page.route("**/api/host", async route => {
      const view = await (await route.fetch()).json();
      const drive = { model: "Example SSD", size: "1 TB", temperature: 40, power_on_hours: 2400, percentage_used: 3, passed: true };
      view.snapshot.smart = [
        { ...drive, device: "/dev/nvme0", media_errors: 0 },
        { ...drive, device: "/dev/nvme1", media_errors: 17 },
      ];
      await route.fulfill({ json: view });
    });
    await page.goto("/host");
    await expect(page.getByText("/dev/nvme0", { exact: true })).toBeHidden();
    await expect(page.getByText("/dev/nvme1", { exact: true })).toBeVisible();
    await expect(page.getByText("17 media errors", { exact: true })).toBeVisible();
    await expectAxeClean(page);
  });
}

/* UX declutter W8: problems stay in sight; healthy units and drives wait behind one quiet line each. */
test("Host shows failed units and drive problems; healthy detail is one line away", async ({ page }) => {
  await page.route("**/api/host", async (route) => {
    const view = await (await route.fetch()).json();
    view.snapshot.services = { units: { accd: "active", caddy: "active", "acc-backup.service": "failed" }, failed: ["acc-backup.service", "acc-gone.service"] };
    const drive = { model: "Example SSD", size: "1 TB", temperature: 40, power_on_hours: 2400, media_errors: 0 };
    view.snapshot.smart = [
      { ...drive, device: "/dev/nvme0", passed: true, percentage_used: 3 },
      { ...drive, device: "/dev/nvme1", passed: false, percentage_used: 12 },
      { ...drive, device: "/dev/sda", passed: true, percentage_used: 61 },
      { ...drive, device: "/dev/sdb", passed: null, percentage_used: null },
    ];
    await route.fulfill({ json: view });
  });
  await page.goto("/host");
  for (const shown of ["acc-backup.service", "acc-gone.service", "/dev/nvme1", "SMART failing", "/dev/sda", "wear 61%", "/dev/sdb", "SMART n/a"])
    await expect(page.getByText(shown, { exact: true })).toBeVisible();
  for (const quiet of ["caddy", "/dev/nvme0"]) await expect(page.getByText(quiet, { exact: true })).toBeHidden();
  const units = page.locator("summary", { hasText: "2 watched units active" });
  const drives = page.locator("summary", { hasText: "1 drive SMART ok" });
  await units.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("caddy", { exact: true })).toBeVisible();
  await drives.click();
  await expect(page.getByText("/dev/nvme0", { exact: true })).toBeVisible();
  await expectAxeClean(page);
});
