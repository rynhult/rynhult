import { wikiShotPath } from "./helpers";
import { expect, test } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
test(`Q15: device login focuses inside while loading, traps Tab and restores focus at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  let finish!: () => void;
  const gate = new Promise<void>((resolve) => { finish = resolve; });
  await page.route("**/api/accounts/codex/login*", async (route) => {
    await gate;
    await route.fulfill({ json: { flow_id: "agent.device", url: "https://example.com/device", user_code: "DEMO-CODE", done: false } });
  });
  await page.route("**/api/accounts/login/agent.device/cancel*", (route) => route.fulfill({ json: { ok: true } }));
  await page.goto("/providers");
  const trigger = page.getByRole("button", { name: "Re-login", exact: true }).nth(1);
  await expect(trigger).toBeEnabled();
  await trigger.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Start re-login" }).dblclick();
  const dialog = page.getByRole("dialog");
  try {
    await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    await page.mouse.click(2, 2);
    await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  } finally { finish(); }
  await expect(dialog.getByRole("link", { name: /example.com\/device/ })).toBeVisible();
  if (width === 390) await dialog.screenshot({ path: wikiShotPath(testInfo, "provider-device-login.png") });
  await trigger.focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("link")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});
}

test("Q14: a late login start is cancelled on its original machine, never shown in a newer dialog", async ({ page }, testInfo) => {
  let finishOld!: () => void;
  const old = new Promise<void>((resolve) => { finishOld = resolve; });
  let starts = 0;
  await page.route("**/api/accounts/codex/login*", async (route) => {
    starts++;
    if (starts === 1) await old;
    const remote = new URL(route.request().url()).searchParams.get("machine") === "lab-box";
    await route.fulfill({ json: { flow_id: remote ? "agent.new" : "agent.old", done: false,
      url: remote ? "https://example.com/new" : "https://example.com/old", user_code: remote ? "NEW-CODE" : "OLD-CODE" } });
  });
  const cancellations: string[] = [];
  await page.route("**/api/accounts/login/*/cancel*", async (route) => {
    cancellations.push(route.request().url());
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto("/providers");
  await page.getByRole("button", { name: "Re-login", exact: true }).nth(1).click();
  await page.getByRole("button", { name: "Start re-login" }).dblclick();
  await expect.poll(() => starts).toBe(1);
  await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  await page.getByRole("radio", { name: /lab-box/ }).click();
  await page.getByRole("button", { name: "Re-login", exact: true }).nth(1).click();
  await page.getByRole("button", { name: "Start re-login" }).dblclick();
  await expect(page.getByText("NEW-CODE", { exact: true })).toBeVisible();
  finishOld();
  await expect.poll(() => cancellations.some((url) => url.endsWith("/agent.old/cancel"))).toBe(true);
  await expect(page.getByText("OLD-CODE", { exact: true })).toHaveCount(0);
  await expect(page.getByText("NEW-CODE", { exact: true })).toBeVisible();
});

test("Q14: device polling is serial and an old poll cannot complete a later machine's dialog", async ({ page }, testInfo) => {
  let finishPoll!: () => void;
  const gate = new Promise<void>((resolve) => { finishPoll = resolve; });
  let polls = 0;
  await page.route("**/api/accounts/codex/login*", (route) => {
    const remote = new URL(route.request().url()).searchParams.get("machine") === "lab-box";
    return route.fulfill({ json: { flow_id: remote ? "agent.new" : "agent.old", done: false,
      url: "https://example.com/device", user_code: remote ? "NEW-CODE" : "OLD-CODE" } });
  });
  await page.route("**/api/accounts/login/agent.old", async (route) => {
    polls++;
    await gate;
    await route.fulfill({ json: { done: true, ok: true } });
  });
  await page.route("**/api/accounts/login/*/cancel*", (route) => route.fulfill({ json: { ok: true } }));
  await page.goto("/providers");
  await page.getByRole("button", { name: "Re-login", exact: true }).nth(1).click();
  await page.getByRole("button", { name: "Start re-login" }).dblclick();
  try {
    await expect.poll(() => polls, { timeout: 10000 }).toBe(1);
    await page.waitForTimeout(3500); // A second poll interval must not overlap the held request.
    expect(polls).toBe(1);
    await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
    await page.getByRole("radio", { name: /lab-box/ }).click();
    await page.getByRole("button", { name: "Re-login", exact: true }).nth(1).click();
    await page.getByRole("button", { name: "Start re-login" }).dblclick();
    await expect(page.getByText("NEW-CODE", { exact: true })).toBeVisible();
  } finally { finishPoll(); }
  await expect(page.getByText("Logged in. Running sessions keep their conversations.")).toHaveCount(0);
  await expect(page.getByText("NEW-CODE", { exact: true })).toBeVisible();
});
