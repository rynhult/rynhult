import { expect, test } from "@playwright/test";
import { AUTH_FILE, routeDemoSessions } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Code-quality review UI-2: loading, a failed first read, a failed refresh with old data, and a genuinely empty list are four states.
test("devices: loading and failure are never 'No devices yet'", async ({ page }) => {
  let mode: "slow" | "fail" | "real" = "slow";
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/push", async (route) => {
    if (mode === "slow") { await held; return route.continue(); }
    if (mode === "fail") return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "broker unavailable" }) });
    return route.continue();
  });
  await page.goto("/settings#devices");
  const devices = page.getByRole("region", { name: "Notification devices", exact: true }).or(page.locator("section").filter({ has: page.getByRole("heading", { name: "Notification devices", exact: true }) }));
  await expect(devices.getByRole("status", { name: "Loading" })).toBeVisible();
  await expect(page.getByText("No notification devices yet")).toHaveCount(0);
  mode = "fail";
  release();
  await page.reload();
  await expect(devices.getByRole("alert")).toContainText("Couldn't read the device list.");
  await expect(page.getByText("No notification devices yet")).toHaveCount(0);
  mode = "real";
  await devices.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("No notification devices yet")).toBeVisible();  // only once the list really arrived empty
  await expect(devices.getByRole("alert")).toHaveCount(0);
});

test("devices: a failed remove is shown at the card", async ({ page }) => {
  const now = new Date().toISOString();
  await page.route("**/api/push", (route) => route.fulfill({ json: { public_key: "", devices: [
    { id: 7, label: "Pixel (Chrome)", endpoint: "https://push.example.com/7", created_at: now, last_ok_at: null, failures: 0 }] } }));
  await page.route("**/api/push/devices/7/remove", (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "disk full" }) }));
  await page.goto("/settings#devices");
  await page.getByRole("button", { name: "Remove Pixel (Chrome)" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Couldn't remove that device." })).toContainText("disk full");
});

test("overview: a failed refresh keeps the sessions with a stale note, and a failed first read is an error", async ({ page }) => {
  await routeDemoSessions(page);  // Own the initial nonempty state; do not depend on earlier spec files.
  await page.goto("/");
  const live = page.locator("section").filter({ has: page.getByRole("heading", { name: "Live sessions" }) });
  await expect(live.getByRole("status", { name: "Loading" })).toHaveCount(0);
  const before = await live.innerText();
  await page.route("**/api/sessions", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "database is busy" }) }));
  await expect(live.getByRole("status").filter({ hasText: "Showing the last data received" })).toBeVisible({ timeout: 15_000 });
  expect(before).not.toContain("No live sessions");
  await page.reload();
  await expect(live.getByRole("alert")).toContainText("Couldn't read the sessions.", { timeout: 15_000 });
  await expect(live.getByText("No live sessions")).toHaveCount(0);
});
