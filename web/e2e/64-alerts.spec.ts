import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { AUTH_FILE, expectAxeClean, sessionWithChanges, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
  test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
  for (const installed of [false, true]) {
    test(`alerts stay compact and actionable at ${width}px${installed ? " in the installed app" : ""}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      await page.addInitScript(() => localStorage.setItem("acc.getStarted", "hidden"));
      await page.route("**/api/preferences/phone-banner", route => route.fulfill({ json: { dismissed: true } }));
      if (installed) await page.addInitScript(() => Object.defineProperty(navigator, "standalone", { value: true }));
      const at = new Date().toISOString();
      const base = { severity: "warn", source: "test", verified: true, detail: "Review this item in Host.",
        session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
        acked_at: null, resolved_at: null, snoozed_until: null, publish_id: null };
      const items = [
        { ...base, id: 6401, kind: "machine_offline", title: "demo-cloud is offline (always-on machine)", persistent: "machine_offline" },
        { ...base, id: 6402, kind: "backup", title: "Rynhult backup failed", persistent: "backup_failed" },
        { ...base, id: 6403, kind: "backup", title: "Rynhult hasn't been backed up in over two hours", persistent: "backup_stale" },
        { ...base, id: 6404, kind: "telemetry_consent", title: "Optional usage statistics and bug reports are available", severity: "info", persistent: null },
        ...Array.from({ length: 15 }, (_, i) => ({ ...base, id: 6500 + i, kind: "host", title: `Demo warning ${i}`, persistent: null })),
      ];
      const snoozes: number[] = [];
      await page.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ json:
        new URL(route.request().url()).searchParams.get("scope") === "all" ? items : items.filter(i =>
          !i.resolved_at && !i.snoozed_until && (i.persistent || !i.acked_at)) }));
      await page.route("**/api/attention/*/ack", route => {
        const item = items.find(i => i.id === Number(route.request().url().split("/").at(-2)))!;
        Object.assign(item, { acked_at: at });
        return route.fulfill({ json: { ok: true } });
      });
      await page.route("**/api/attention/*/snooze", route => {
        const minutes = route.request().postDataJSON().minutes;
        snoozes.push(minutes);
        Object.assign(items.find(i => i.id === 6401)!, { snoozed_until: minutes ? new Date(Date.now() + minutes * 60_000).toISOString() : null });
        return route.fulfill({ json: { ok: true } });
      });
      await page.goto("/");
      const status = page.getByRole("region", { name: "Inbox status" });
      await expect(status).toBeVisible();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(status).toContainText("Machine offline: 1");
      await expect(status).toContainText("Backup failed: 1");
      await expect(status).toContainText("Backup stale: 1");
      const box = (await status.boundingBox())!;
      expect(box.height).toBeLessThanOrEqual(96);
      const main = page.getByRole("main");
      await expect(main.getByRole("heading", { name: "Overview", exact: true })).toBeInViewport();
      // The old four-row Inbox stack displaced the phone's stats and actionable content.
      await expect(main.getByRole("link", { name: /^Inbox/ })).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expectAxeClean(page);
      if (!installed) {
        await page.screenshot({ path: wikiShotPath(testInfo, width === 390 ? "phone-overview.png" : "overview.png") });
        await page.screenshot({ path: wikiShotPath(testInfo, width === 390 ? "inbox-status-phone.png" : "inbox-status-desktop.png") });
      }
      const id = await sessionWithChanges(page, `alerts-${width}`);
      await page.goto(`/sessions/${id}`);
      await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
      for (const label of ["Machine offline: 1", "Backup failed: 1", "Backup stale: 1"]) {
        await expect(status.getByRole("link", { name: label, exact: true })).toBeInViewport();
      }
      await expect(page.locator(".xterm")).toBeVisible();
      const terminal = (await page.locator(".xterm").boundingBox())!;
      const statusBox = (await status.boundingBox())!;
      if (width === 390) {
        await expect(page.locator(".xterm")).toBeInViewport({ ratio: 1 });
        expect(terminal.height).toBeGreaterThanOrEqual(200);
        expect(statusBox.y).toBeGreaterThan(terminal.y + terminal.height);
        await expect(page.getByRole("button", { name: "Enter", exact: true })).toBeInViewport();
        if (!installed) await page.screenshot({ path: wikiShotPath(testInfo, "phone-session-alerts.png") });
      } else {
        expect(statusBox.y + statusBox.height).toBeLessThan(terminal.y);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expectAxeClean(page);
      await status.getByRole("button", { name: "Collapse other notices" }).click();
      await expect(status).toContainText("Machine offline: 1");
      await expect(status).toContainText("Backup failed: 1");
      await expect(status).toContainText("Backup stale: 1");
      await expect(status.getByText("16 other notices")).toBeHidden();
      await status.getByRole("link", { name: /Inbox/ }).click();
      await expect(main.getByRole("heading", { name: "Inbox", exact: true })).toBeVisible();
      if (!installed && width === 1440) await page.screenshot({ path: wikiShotPath(testInfo, "inbox-operational-alerts.png") });
      await main.getByRole("link", { name: "Review backups" }).first().click();
      await expect(page.locator("#host-backups").getByRole("heading", { name: "Backups", exact: true })).toBeInViewport();
      await status.getByRole("link", { name: /Inbox/ }).click();
      const row = main.getByText("demo-cloud is offline (always-on machine)").locator("xpath=ancestor::div[contains(@class,'grid')][1]");
      await row.getByRole("button", { name: "Acknowledge", exact: true }).click();
      await expect(row.getByText("acknowledged", { exact: true })).toBeVisible();
      await expect(status).toContainText("Machine offline: 1");
      await row.getByRole("button", { name: "Snooze 1 hour" }).click();
      await expect(row).toBeHidden();
      await expect(status.getByText("Machine offline: 1")).toBeHidden();
      await expect(status).toContainText("Backup failed: 1");
      await expect(status).toContainText("Backup stale: 1");
      await main.getByRole("radio", { name: "History" }).click();
      await row.getByRole("button", { name: "End snooze" }).click();
      await expect(status).toContainText("Machine offline: 1");
      expect(snoozes).toEqual([60, 0]);
      await row.getByRole("button", { name: "Snooze 1 hour" }).click();
      await main.getByRole("radio", { name: "Open", exact: true }).click();
      const consent = main.getByText("Optional usage statistics and bug reports are available").locator("xpath=ancestor::div[contains(@class,'grid')][1]");
      await expect(consent.getByRole("link", { name: "Review privacy choices" })).toHaveAttribute("href", "/settings#telemetry");
      await consent.getByRole("button", { name: "Dismiss notice" }).click();
      await expect(consent).toBeHidden();
      Object.assign(items[1], { resolved_at: at });
      Object.assign(items[2], { resolved_at: at });
      await expect(status).toBeHidden();
      await expectAxeClean(page);
    });
  }
  test(`the optional telemetry invitation alone never blocks ${width}px content`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("acc.getStarted", "hidden"));
    const at = new Date().toISOString();
    const consent = { id: 6404, kind: "telemetry_consent", title: "Optional usage statistics and bug reports are available",
      severity: "info", source: "telemetry", verified: true, detail: "Both choices are off by default.",
      session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
      acked_at: null, resolved_at: null, publish_id: null, persistent: null };
    await page.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ json: [consent] }));
    await page.route(/\/api\/telemetry(?:\?.*)?$/, async route => {
      const response = await route.fetch();
      const state = await response.json();
      await route.fulfill({ response, json: { ...state, consent: { ...state.consent, usage: false, reports: false } } });
    });
    await page.goto("/");
    const status = page.getByRole("region", { name: "Inbox status" });
    await expect(status.getByRole("link", { name: "Inbox (1)" })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect((await status.boundingBox())!.height).toBeLessThanOrEqual(64);
    await status.getByRole("button", { name: "Collapse other notices" }).click();
    await expect(status).toBeHidden();
    await page.goto("/inbox");
    await page.getByRole("main").getByRole("link", { name: "Review privacy choices" }).click();
    await expect(page.locator("#telemetry")).toBeFocused();
    await expect(page.locator("#telemetry").getByRole("heading", { name: "Usage and bug reports", exact: true })).toBeInViewport();
    await expect(page.getByLabel("Share usage statistics")).toBeVisible();
    await expect(page.getByLabel("Offer bug reports for review")).toBeVisible();
    await expect(page.getByLabel("Share usage statistics")).not.toBeChecked();
    await expect(page.getByLabel("Offer bug reports for review")).not.toBeChecked();
  });
  });
}

for (const role of ["owner", "member"]) {
  test(`the ${role} Overview leaves content visible without an alert stack`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.addInitScript((theme) => {
      localStorage.setItem("acc-theme", theme);
      localStorage.setItem("acc.getStarted", "hidden");
    }, role === "owner" ? "light" : "dark");
    await page.route("**/api/auth/state", route => route.fulfill({ json: {
      authenticated: true, setup_required: false, username: "demo-person", role, account: "demo-agent",
      instance: { host: "acc-host" },
    } }));
    for (const path of ["attention", "sessions", "projects"]) {
      await page.route(new RegExp(`/api/${path}(?:\\?.*)?$`), route => route.fulfill({ json: [] }));
    }
    await page.goto("/");
    await expect(page.getByRole("main").getByRole("heading", { name: "Overview", exact: true })).toBeInViewport();
    await expect(page.getByRole("region", { name: "Inbox status" })).toBeHidden();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expectAxeClean(page);
    await page.screenshot({ path: wikiShotPath(testInfo, role === "owner" ? "overview-light.png" : "member-overview.png") });
  });
}

test("service worker avoids foreground overlays, even for a critical push", async () => {
  for (const visible of [true, false]) {
    let pushed: ((event: unknown) => void) | undefined;
    const notifications: { title: string; options: { requireInteraction: boolean } }[] = [];
    vm.runInNewContext(readFileSync("public/sw.js", "utf8"), {
      self: {
        addEventListener: (name: string, listener: typeof pushed) => { if (name === "push") pushed = listener; },
        clients: { matchAll: async () => [{ visibilityState: visible ? "visible" : "hidden", url: "https://acc.example.com/" }] },
        registration: { showNotification: async (title: string, options: { requireInteraction: boolean }) => { notifications.push({ title, options }); } },
        location: { origin: "https://acc.example.com" },
      },
      URL,
    });
    let done: Promise<unknown> | undefined;
    pushed!({ data: { json: () => ({ title: "Backup failed", critical: true }) }, waitUntil: (p: Promise<unknown>) => { done = p; } });
    await done;
    expect(notifications).toHaveLength(visible ? 0 : 1);
    if (!visible) expect(notifications[0].options.requireInteraction).toBe(false);
  }
});
