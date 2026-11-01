import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, openAdvanced, waitForSessionState, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("changing machine clears single and several-agent models from the previous machine", async ({ page }) => {
  const { id } = await gitProject(page, "machine-models");
  await page.route("**/api/models?**", route => {
    const remote = new URL(route.request().url()).searchParams.get("machine") === "lab-box";
    return route.fulfill({ json: { claude: [{ id: remote ? "node-model" : "main-model", label: "Scoped model", source: "CLI" }] } });
  });
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
  await openAdvanced(dialog);
  const machine = dialog.getByRole("combobox", { name: "Machine", exact: true });
  const model = dialog.getByRole("combobox", { name: "Model", exact: true });
  await expect(model.locator('option[value="main-model"]')).toHaveCount(1);
  await model.selectOption("main-model");
  await machine.selectOption("lab-box");
  await expect(model.locator('option[value="node-model"]')).toHaveCount(1);
  await expect(model).toHaveValue("");
  await machine.selectOption("local");
  await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Compare implementations");
  await dialog.getByRole("radio", { name: /Several agents/ }).check();
  const runModel = dialog.getByRole("combobox", { name: "Run 1: Model", exact: true });
  await expect(runModel.locator('option[value="main-model"]')).toHaveCount(1);
  await runModel.selectOption("main-model");
  const preview = page.waitForRequest(request => request.url().endsWith("/api/sessions/preview")
    && request.postDataJSON()?.machine === "lab-box" && request.postDataJSON()?.provider === "claude");
  await machine.selectOption("lab-box");
  expect((await preview).postDataJSON().model).toBe("");
  await expect(runModel).toHaveValue("");
});

for (const width of [1440, 390]) {
  test(`machine selection ${width}px: ordinary launch confirms clone and reuses the node checkout`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const { id } = await gitProject(page, "machine-choice");
    let launched: string | undefined;
    try {
      await page.goto("/sessions");
      await page.getByRole("button", { name: "New session", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "New session", exact: true });
      await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
      const machine = dialog.getByRole("combobox", { name: "Machine", exact: true });
      await expect(machine).toHaveValue("local");
      await machine.selectOption("lab-box");
      await expect(dialog.getByRole("button", { name: "Clone on lab-box", exact: true })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeDisabled();
      await dialog.getByRole("button", { name: "Clone on lab-box", exact: true }).click();
      await expect(dialog.getByText(/Runs on lab-box as/)).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeEnabled();
      await expectAxeClean(page, "[role=dialog]");
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
      await dialog.screenshot({ path: wikiShotPath(testInfo, `new-session-machine${width === 390 ? "-phone" : ""}.png`) });
      const response = page.waitForResponse(r => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
      await dialog.getByRole("button", { name: "Launch", exact: true }).click();
      const created = await response;
      expect(created.ok()).toBe(true);
      expect(created.request().postDataJSON()).toMatchObject({ machine: "lab-box", clone_on_machine: true });
      const session = await created.json();
      launched = session.id;
      expect(session.host).toBe("lab-box");
      expect(session.worktree).toBeTruthy();
      await waitForSessionState(page, session.id, "idle", "claude_hook");
      await page.getByRole("button", { name: "New session", exact: true }).click();
      await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
      await dialog.getByRole("combobox", { name: "Machine", exact: true }).selectOption("lab-box");
      await expect(dialog.getByText("Using an existing checkout on lab-box.", { exact: true })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Clone on lab-box", exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeEnabled();
    } finally {
      if (launched) await page.request.post(`/api/sessions/${launched}/stop`, { headers: await csrfHeaders(page) });
    }
  });

  test(`machine selection ${width}px: team machines are keyboard checkboxes and unavailable agents explain why`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const { id } = await gitProject(page, "machine-grant");
    await page.route("**/api/providers/registry?**", async route => {
      const data = await (await route.fetch()).json();
      if (new URL(route.request().url()).searchParams.get("machine") === "lab-box") {
        data.providers = data.providers.map((p: { id: string }) => ({ ...p, ...(p.id === "codex" ? { installed: false } : {}) }));
      }
      await route.fulfill({ json: data });
    });
    await page.goto("/sessions");
    await page.getByRole("button", { name: "New session", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "New session", exact: true });
    await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
    await dialog.getByRole("combobox", { name: "Machine", exact: true }).selectOption("lab-box");
    await expect(dialog.getByRole("combobox", { name: "Agent", exact: true }).locator('option[value="codex"]')).toHaveJSProperty("disabled", true);
    await expect(dialog.getByRole("combobox", { name: "Agent", exact: true }).locator('option[value="codex"]')).toContainText("not installed on lab-box");
    await dialog.getByRole("combobox", { name: "Machine", exact: true }).selectOption("local");
    await openAdvanced(dialog);
    await dialog.getByRole("radio", { name: /With helpers/ }).check();
    const machines = dialog.getByRole("group", { name: "Machines", exact: true });
    await expect(machines.getByRole("checkbox", { name: /This Rynhult \(main\)/ })).toBeChecked();
    const remote = machines.getByRole("checkbox", { name: /lab-box/ });
    const updated = page.waitForResponse(r => r.url().endsWith("/api/sessions/preview")
      && r.request().postDataJSON()?.orchestrator?.machines?.includes("lab-box") && r.ok());
    await remote.focus();
    await page.keyboard.press("Space");
    await expect(remote).toBeChecked();
    await expect(machines).toContainText("Online");
    await expect(machines).toContainText("Linux");
    await expect(machines).toContainText("Claude Code");
    await expectAxeClean(page, "[role=dialog]");
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await machines.screenshot({ path: wikiShotPath(testInfo, `orchestrator-machines${width === 390 ? "-phone" : ""}.png`) });
    const preview = await updated;
    expect(preview.request().postDataJSON().orchestrator.machines).toEqual(["local", "lab-box"]);
  });
}
