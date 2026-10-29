import { expect, test } from "@playwright/test";
import fs from "node:fs";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { AUTH_FILE, control, gitProject, gotoAppPage, info, waitForSessionState } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("a fake CLI never observes a partially published control command", async () => {
  const id = `control-${randomUUID()}`;
  const folder = `${info().run_dir}/${id}`;
  const published = `${folder}/fake.ctl`;
  fs.mkdirSync(folder);
  const write = fs.writeFileSync;
  const observed: string[] = [];
  fs.writeFileSync = (file, data, options) => {
    if (String(file).startsWith(`${folder}/`)) {
      write(file, "");
      if (fs.existsSync(published)) observed.push(fs.readFileSync(published, "utf8"));
    }
    return write(file, data, options);
  };
  syncBuiltinESMExports();
  try {
    control(id, "permission");
    expect(observed).toEqual([]);
    expect(fs.readFileSync(published, "utf8")).toBe("permission");
  } finally {
    fs.writeFileSync = write;
    syncBuiltinESMExports();
    fs.rmSync(folder, { recursive: true });
  }
});

test("project fixture prefixes are reusable without sharing a checkout or project", async ({ page }) => {
  const prefix = `repeatable-${randomUUID().slice(0, 8)}`;
  const first = await gitProject(page, prefix);
  const creatingSecond = gitProject(page, prefix);
  await expect(creatingSecond).resolves.toEqual(expect.objectContaining({ id: expect.any(Number) }));
  const second = await creatingSecond;
  expect(second.id).not.toBe(first.id);
  expect(second.folder).not.toBe(first.folder);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "New session", exact: true })
    .getByRole("combobox", { name: "Project", exact: true })).toContainText(second.slug);
});

test("session readiness waits for both the requested state and its provenance", async ({ page }) => {
  const id = `readiness-${randomUUID()}`;
  const observations = [
    { observed_state: "active", status_source: "screen" },
    { observed_state: "idle", status_source: "screen" },
    { observed_state: "idle", status_source: "codex_hook" },
  ];
  let requests = 0;
  await page.route(`**/api/sessions/${id}`, (route) => route.fulfill({
    json: observations[Math.min(requests++, observations.length - 1)],
  }));
  await page.goto("/sessions");
  await waitForSessionState(page, id, "idle", "codex_hook");
  expect(requests).toBe(3);
});

for (const shellReady of [false, true]) {
  test(`page navigation waits for its ${shellReady ? "lazy route" : "shell"}`, async ({ page }) => {
    const loading = '<div role="status">Loading page&#8230;</div>';
    await page.route("**/fixture-loading", route => route.fulfill({
      contentType: "text/html",
      body: shellReady ? `<main>${loading}</main>` : loading,
    }));
    let settled = false;
    const navigating = gotoAppPage(page, "/fixture-loading").then(() => { settled = true; });
    await expect(page.getByRole("status")).toBeVisible();
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(settled).toBe(false);
    await page.evaluate(() => {
      const main = document.createElement("main");
      main.textContent = "Ready";
      document.body.replaceChildren(main);
    });
    await navigating;
    await expect(page.getByRole("main")).toHaveText("Ready");
  });
}

test("helper fixture flags follow the shared environment-name precedence", () => {
  const inherited = { ...process.env };
  for (const key of ["ACC_E2E_DEVICE_CA", "ACC_E2E_REMOTE_EDGE", "RYNHULT_E2E_DEVICE_CA", "RYNHULT_E2E_REMOTE_EDGE"]) {
    delete inherited[key];
  }
  for (const scenario of [
    { flags: {}, expected: ["1", "1"] },
    { flags: { ACC_E2E_DEVICE_CA: "0", ACC_E2E_REMOTE_EDGE: "0" }, expected: ["0", "0"] },
    { flags: { ACC_E2E_DEVICE_CA: "1", ACC_E2E_REMOTE_EDGE: "1",
      RYNHULT_E2E_DEVICE_CA: "0", RYNHULT_E2E_REMOTE_EDGE: "0" }, expected: ["0", "0"] },
  ]) {
    const script = `await import(${JSON.stringify(new URL("../playwright.config.ts", import.meta.url).href)});
      console.log(JSON.stringify([process.env.ACC_E2E_DEVICE_CA, process.env.ACC_E2E_REMOTE_EDGE]));`;
    const result = execFileSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", script],
      { env: { ...inherited, ...scenario.flags }, encoding: "utf8" });
    expect(JSON.parse(result)).toEqual(scenario.expected);
  }
});
