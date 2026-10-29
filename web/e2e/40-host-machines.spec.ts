import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, wikiShotPath } from "./helpers";

/* Host details for every machine: the Machine picker shows a connected node's details, reported over its link
   (lab-box: the e2e server's demo machine, tests/demo_machine.py, artificial numbers). */
test.use({ storageState: AUTH_FILE });

for (const target of ["Host", "Glance"]) {
  test(`${target} labels the Mac data disk without changing its real mount`, async ({ page }, testInfo) => {
    await page.route("**/api/host?machine=lab-box", async (route) => {
      const view = await (await route.fetch()).json();
      view.snapshot.system = "mac";
      view.snapshot.disks = [
        { mount: "/System/Volumes/Data", device: "/dev/disk3s5", total: 100e9, used: 25e9, free: 75e9, percent: 25 },
        { mount: "/Volumes/External", device: "/dev/disk10s1", total: 100e9, used: 40e9, free: 60e9, percent: 40 },
      ];
      await route.fulfill({ json: view });
    });
    await page.route("**/api/board", async (route) => {
      const view = await (await route.fetch()).json();
      view.machines[1].light.disk = { mount: "/System/Volumes/Data", percent: 25, free: 75e9 };
      await route.fulfill({ json: view });
    });
    await page.goto(target === "Host" ? "/host?machine=lab-box" : "/board");
    const label = page.getByText("Macintosh HD (data)", { exact: true });
    await expect(label).toBeVisible();
    await expect(label).toHaveAttribute("title", "/System/Volumes/Data");
    await expect(page.getByRole("meter", { name: `${target === "Host" ? "" : "Disk "}Macintosh HD (data) used`, exact: true }))
      .toHaveAttribute("aria-valuenow", "25");
    if (target === "Host") {
      await expect(page.getByText("/Volumes/External", { exact: true })).toBeVisible();
      await page.getByRole("heading", { name: "Storage", exact: true }).locator("xpath=ancestor::section[1]")
        .screenshot({ path: wikiShotPath(testInfo, "host-macos-data.png") });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(label).toBeVisible();
    expect(await label.locator("..").evaluate((element) => element.getBoundingClientRect().right)).toBeLessThanOrEqual(390);
  });
}

test("Host's Machine picker shows another machine's details, and what it can't report", async ({ page }, testInfo) => {
  await page.goto("/host");
  const picker = page.getByRole("radiogroup", { name: "Machine" });
  await expect(picker.getByRole("radio", { name: /acc-host.*main/ })).toHaveAttribute("aria-checked", "true");
  await picker.getByRole("radio", { name: /lab-box/ }).click();
  await expect(page).toHaveURL(/\/host\?machine=lab-box$/);
  await expect(page.getByText("lab-box · 6.8.0-45-generic")).toBeVisible();
  await expect(page.getByText(/Reported by lab-box/)).toBeVisible();
  await expect(page.getByText("/srv", { exact: true })).toBeVisible();
  await expect(page.getByRole("meter", { name: "/srv used" })).toHaveAttribute("aria-valuenow", "88");
  await expect(page.getByText("acc-node", { exact: true })).toBeHidden();  // healthy units wait behind one line
  await page.locator("summary", { hasText: "4 watched units active" }).click();
  await expect(page.getByText("acc-node", { exact: true })).toBeVisible();
  await expect(page.getByText(/SMART drive health needs the root collector/)).toBeVisible();  // named, not zero
  await expect(page.getByText("No NVIDIA GPU reported")).toBeVisible();
  await expect(page.getByText("Containers", { exact: true })).toHaveCount(0);  // root-only parts aren't faked
  await expect(page.getByRole("heading", { name: "Machines" })).toHaveCount(0);
  // Refresh asks the machine again (at most every 15 s; the demo machine's CPU moves with each answer)
  const cpu = page.getByRole("meter", { name: "CPU" });
  await expect(cpu).toHaveAttribute("aria-valuenow", /^2[3-7]$/);
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, "host-machine-picker.png") });
  await picker.getByRole("radio", { name: /acc-host.*main/ }).click();
  await expect(page).toHaveURL(/\/host$/);
  await expect(page.getByRole("heading", { name: "Machines" })).toBeVisible();
});

test("Host details link from the Machines card; an older machine says to update it", async ({ page }) => {
  await page.goto("/host");
  await page.getByRole("link", { name: "Host details of lab-box" }).click();
  await expect(page).toHaveURL(/\/host\?machine=lab-box$/);
  await page.route("**/api/host?machine=lab-box", (route) => route.fulfill({ json: {
    machine: "lab-box", connected: true, supported: false, version: "0.11.0", os: "linux", last_seen: new Date().toISOString(),
    snapshot: null, live: null, received_at: null, light: null } }));
  await page.reload();
  await expect(page.getByText("lab-box runs Rynhult 0.11.0, which doesn't report host details. Update this machine to see them here.")).toBeVisible();
  await page.getByRole("button", { name: "Update this machine" }).click();
  await expect(page.getByRole("dialog", { name: "Update lab-box?" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByText("CPU", { exact: true })).toHaveCount(0);  // nothing shown as 0%
  await expectAxeClean(page);
  await page.route("**/api/host?machine=lab-box", (route) => route.fulfill({ json: {
    machine: "lab-box", connected: false, supported: false, version: "0.13.0", os: "linux", last_seen: new Date(Date.now() - 7200e3).toISOString(),
    snapshot: null, live: null, received_at: null, light: null } }));
  await page.reload();
  await expect(page.getByText("lab-box is offline (last seen 2h ago). Its details appear once it connects again.")).toBeVisible();
});

test("a member's Host has no Machine picker and never asks for another machine", async ({ page }) => {
  // The server refuses members too (tests/test_node_host.py); here: the page doesn't ask.
  let asked = 0;
  await page.route("**/api/auth/state", (r) => r.fulfill({ json: { authenticated: true, setup_required: false,
    username: "demo-member", role: "member", account: "demo-agent", instance: { host: "acc-host" } } }));
  await page.route(/\/api\/(nodes|host\?machine=)/, (r) => { asked++; return r.fulfill({ status: 403, json: { detail: "owners only" } }); });
  await page.goto("/host?machine=lab-box");
  await expect(page.getByText("CPU", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("radiogroup", { name: "Machine" })).toHaveCount(0);
  await expect(page.getByText(/lab-box/)).toHaveCount(0);
  expect(asked).toBe(0);
});

// SR5-02: a compromised machine must not take down the owner's Host view with its timestamp.
test("Host survives an unrenderable node timestamp and keeps reported text inert", async ({ page }, testInfo) => {
  await page.route("**/api/host?machine=lab-box", async (route) => {
    const response = await route.fetch();
    const view = await response.json();
    view.snapshot.collected_at = 1e18;
    view.snapshot.kernel = '<img src=x onerror=alert(1)>';
    await route.fulfill({ json: view });
  });
  await page.goto("/host?machine=lab-box");
  await expect(page.getByText(/Reported by lab-box/)).toBeVisible();
  await expect(page.getByText(/Collection time unavailable/)).toBeVisible();
  await expect(page.getByText('<img src=x onerror=alert(1)>', { exact: false })).toBeVisible();
  await expect(page.locator('main img')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('host-invalid-time.png') });
});

for (const target of ["Host", "board"]) test(`${target} contains long node strings at phone width`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/host?machine=lab-box", async (route) => {
    const response = await route.fetch();
    const view = await response.json();
    view.snapshot.gpu = [{ name: 'W'.repeat(200), utilization: 1, memory_used_mb: 1, memory_total_mb: 2, temperature: 30 }];
    await route.fulfill({ json: view });
  });
  if (target === "Host") {
    await page.goto("/host?machine=lab-box");
    const gpu = page.getByText('W'.repeat(200), { exact: true });
    await page.locator("summary", { hasText: "GPU" }).click();  // a reported GPU is healthy detail
    await expect(gpu).toBeVisible();
    expect(await gpu.evaluate((el) => el.getBoundingClientRect().right)).toBeLessThanOrEqual(390);
  } else {
    await page.route("**/api/board", async (route) => {
      const response = await route.fetch();
      const view = await response.json();
      view.machines[1].alerts = [{ severity: 'warn', title: 'W'.repeat(200) }];
      view.machines[1].version = 'W'.repeat(80);
      await route.fulfill({ json: view });
    });
    await page.goto("/board");
    const machine = page.locator('[data-machine="lab-box"]');
    await expect(machine).toBeVisible();
    expect(await machine.evaluate((el) => el.scrollWidth)).toBeLessThanOrEqual(390);
  }
  await page.screenshot({ path: testInfo.outputPath(`${target.toLowerCase()}-long-node-text.png`) });
});

test("Host contains long node identity and service state strings", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/host?machine=lab-box", async (route) => {
    const view = await (await route.fetch()).json();
    view.snapshot.hostname = 'H'.repeat(120);
    view.snapshot.kernel = 'K'.repeat(120);
    view.snapshot.services = { units: { example: 'W'.repeat(32) }, failed: [] };
    await route.fulfill({ json: view });
  });
  await page.goto("/host?machine=lab-box");
  await expect(page.getByText('W'.repeat(32), { exact: true })).toBeVisible();
  const state = page.getByText('W'.repeat(32), { exact: true });
  expect(await state.evaluate((el) => el.getBoundingClientRect().right)).toBeLessThanOrEqual(390);
  const identity = page.getByText('K'.repeat(120), { exact: false });
  expect(await identity.evaluate((el) => { const range = document.createRange(); range.selectNodeContents(el); return range.getBoundingClientRect().right; })).toBeLessThanOrEqual(390);
});

for (const connected of [true, false]) test(`Host keeps the last public backup visible when connected=${connected}`, async ({ page }, testInfo) => {
  await page.route("**/api/host?machine=lab-box", async (route) => {
    const view = await (await route.fetch()).json();
    view.last_backup_via_public_address = "2026-10-25T10:00:00+00:00";
    view.connected = connected;
    if (!connected) view.snapshot = null;
    await route.fulfill({ json: view });
  });
  await page.goto("/host?machine=lab-box");
  const note = page.getByText(/Last backup via public address/);
  await expect(note).toBeVisible();
  await expect(note.locator("time")).toHaveAttribute("dateTime", "2026-10-25T10:00:00+00:00");
  if (connected) {
    await page.screenshot({ path: wikiShotPath(testInfo, "host-machine-public-backup.png") });
    await expectAxeClean(page);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(note).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`public-backup-${connected}-phone.png`) });
});
