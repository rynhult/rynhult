import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Owner request: a Windows PC's WSL machine reads apart from a plain Linux one. lab-box is plain Linux, studio-mac a
// Mac (tests/demo_machine.py); win-pc (WSL2 on Windows 11) and old-box (a Rynhult from before WSL was reported) are
// added to the machine list as the server would describe them (tests/test_multihost.py covers the real path).
test.beforeEach(async ({ page }) => {
  await page.route("**/api/nodes", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const nodes = await (await route.fetch()).json();
    const like = nodes.find((n: { name: string }) => n.name === "lab-box");
    nodes.push({ ...like, id: "win", name: "win-pc", software: null, logins: {},
      info: { ...like.info, hostname: "win-pc", wsl: "2", windows: "Windows 11 (build 22631)" },
      system: { kind: "wsl", label: "Linux (WSL2) · Windows 11 (build 22631)", wsl: "2", windows: "Windows 11 (build 22631)" } });
    nodes.push({ ...like, id: "old", name: "old-box", connected: false, software: null, logins: {},
      info: { os: "linux", arch: "x86_64", hostname: "old-box" }, system: { kind: "unknown", label: "Linux (WSL or not: unknown)" } });
    await route.fulfill({ json: nodes });
  });
});

test("Host → Machines tells WSL, Linux, Mac and unknown apart", async ({ page }) => {
  await page.goto("/host");
  const row = (name: string) => page.locator("div.flex-wrap").filter({ has: page.getByText(name, { exact: true }) }).first();
  await expect(row("win-pc").getByText("WSL2", { exact: true })).toBeVisible();
  await expect(row("win-pc").getByText("Linux (WSL2) · Windows 11 (build 22631) · x86_64")).toBeVisible();
  await expect(row("lab-box").getByText("Linux · x86_64", { exact: true })).toBeVisible();
  await expect(row("lab-box").getByText("WSL2", { exact: true })).toHaveCount(0);
  await expect(row("studio-mac").getByText("macOS · arm64", { exact: true })).toBeVisible();
  await expect(row("old-box").getByText("Linux (WSL or not: unknown) · x86_64")).toBeVisible();
  await expect(page.getByText("linux/x86_64")).toHaveCount(0);  // no bare os/arch any more
  await expectAxeClean(page, "main");
});

test("the machine picker and Providers show the WSL badge", async ({ page }) => {
  await page.goto("/providers?machine=win-pc");
  const picker = page.getByRole("radiogroup", { name: "Machine" });
  await expect(picker.getByRole("radio", { name: /win-pc/ })).toContainText("WSL2");
  await expect(picker.getByRole("radio", { name: /lab-box/ })).toContainText("Linux");
  await expect(page.getByText("Linux (WSL2) · Windows 11 (build 22631) · x86_64")).toBeVisible();
});

test("node-reported system strings stay inert text", async ({ page }) => {
  const diagnostic = '<img src=x onerror="window.__nodeMarkupRan=true">';
  await page.route("**/api/nodes", async (route) => {
    const nodes = await (await route.fetch()).json();
    nodes.find((n: { name: string }) => n.name === "lab-box").system = { kind: "wsl", label: diagnostic, wsl: "2", windows: diagnostic };
    await route.fulfill({ json: nodes });
  });
  await page.goto("/host");
  await expect(page.getByText(`${diagnostic} · x86_64`, { exact: true })).toBeVisible();
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__nodeMarkupRan)).toBeUndefined();
});
