import { expect, test } from "@playwright/test";
import { AUTH_FILE, openAdvanced } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("Q13: switching projects reloads provider availability and models", async ({ page }) => {
  await page.route("**/api/projects", async (route) => {
    const project = { slug: "demo", workspace_path: "/demo", repo: null, import_mode: "managed", trusted: true, archived: false,
      notes: null, compose_autostart: false, git_backup: false };
    await route.fulfill({ json: [{ ...project, id: 801, name: "Local demo", host: "local" },
      { ...project, id: 802, name: "Node demo", host: "lab-box" }] });
  });
  // Snapshot the fixture server's contract before intercepting browser requests.
  // A project switch can cancel an old scoped request; its route handler must not
  // leave a route.fetch() response to be read after Playwright disposes the test.
  const registryResponse = await page.request.get("/api/providers/registry");
  expect(registryResponse.ok()).toBe(true);
  const data = await registryResponse.json();
  await page.route("**/api/providers/registry*", async (route) => {
    const remote = new URL(route.request().url()).searchParams.get("project_id") === "802";
    await route.fulfill({ json: { providers: data.providers.map((p: { id: string }) => ({ ...p, installed: p.id === (remote ? "codex" : "claude") })) } });
  });
  await page.route("**/api/models*", (route) => route.fulfill({ json: {
    codex: [{ id: new URL(route.request().url()).searchParams.get("project_id") === "802" ? "node-model" : "local-model", label: "Scoped model", source: "CLI" }],
  } }));
  await page.route("**/api/sessions/preview", (route) => route.fulfill({ json: {
    command: ["agent"], cwd: "/demo", files: {}, digest: "demo", trusted: true, import_mode: "managed",
  } }));
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("801");
  await expect(page.getByRole("combobox", { name: "Agent", exact: true }).locator('option[value="codex"]')).toHaveAttribute("disabled", "");
  await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("802");
  await expect(page.getByRole("combobox", { name: "Agent", exact: true }).locator('option[value="codex"]')).not.toHaveAttribute("disabled");
  await page.getByRole("combobox", { name: "Agent", exact: true }).selectOption("codex");
  await openAdvanced(page.getByRole("dialog"));  // the model list is under Advanced
  await expect(page.getByRole("combobox", { name: "Model", exact: true }).locator('option[value="node-model"]')).toHaveCount(1);
  await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("node-model");
  await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("801");
  await expect(page.getByRole("combobox", { name: "Agent", exact: true }).locator('option[value="codex"]')).toHaveAttribute("disabled", "");
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveValue("");
  await expect(page.getByRole("combobox", { name: "Model", exact: true }).locator('option[value="node-model"]')).toHaveCount(0);
});
