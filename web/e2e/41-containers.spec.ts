import { expect, test } from "@playwright/test";
import { existsSync, renameSync } from "node:fs";
import path from "node:path";
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, openAdvanced, wikiShotPath } from "./helpers";

// Containers are a Preview that needs a qualified host. The e2e server records a passing qualification report for its
// host at start (tests/e2e_server.py, qualify_containers): the real record and check, its own uid standing in for root.
const qualificationRecord = () => path.join(info().state, "qualified-host/etc/acc/containers/host-qualified.json");

test.use({ storageState: AUTH_FILE });

test("owners set container limits in Settings; owners' accounts start unlimited", async ({ page }, testInfo) => {
  await page.goto("/settings#container-limits");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Container limits" }) });
  await expect(card).toBeVisible();
  await expect(card.getByRole("heading", { name: "Container limits Preview" })).toBeVisible();  // the release label
  const form = card.getByRole("form").first();
  await expect(form.getByText("owners aren't limited unless you set limits")).toBeVisible();
  await expect(form.getByText("No limits")).toBeVisible();
  const memory = form.getByLabel("Memory (GiB)");
  await expect(memory).toHaveValue("");
  await expect(memory).toHaveAttribute("placeholder", "No limit");
  await card.screenshot({ path: wikiShotPath(testInfo, "settings-container-limits.png") });
  await memory.fill("8");
  await form.getByLabel("Sessions at once").fill("6");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByText("8 GiB memory · 6 sessions")).toBeVisible();
  await page.reload();
  await expect(card.getByRole("form").first().getByLabel("Memory (GiB)")).toHaveValue("8");
  await expectAxeClean(page, "main");
  // Back to no limits: empty fields
  const again = card.getByRole("form").first();
  await again.getByLabel("Memory (GiB)").fill("");
  await again.getByLabel("Sessions at once").fill("");
  await again.getByRole("button", { name: "Save" }).click();
  await expect(again.getByText("No limits")).toBeVisible();
});

test("owners switch a project's new sessions to containers, and it is audited", async ({ page }, testInfo) => {
  const { id } = await gitProject(page, `boxed-${Date.now()}`);
  const containers = await (await page.request.get("/api/containers")).json();
  expect(containers.qualification.passed, containers.qualification.reason).toBe(true);  // this host's passing record
  await page.goto(`/projects/${id}#project-stack`);
  const toggle = page.getByRole("checkbox", { name: "Run this project's new sessions in containers" });
  await expect(toggle).not.toBeChecked();
  await expect(page.getByText("Off: sessions run as they always have.")).toBeVisible();
  await expect(page.getByText("Containers are a Preview in this release")).toBeVisible();
  await toggle.check();
  await expect(page.getByText("Each new session works in a checkout of its own")).toBeVisible();
  await page.reload();
  await expect(toggle).toBeChecked();
  const card = page.locator("#project-stack").locator("section").filter({ has: page.getByRole("heading", { name: "Containers Preview", exact: true }) });
  await card.screenshot({ path: wikiShotPath(testInfo, "projects-containers.png") });
  await toggle.uncheck();
  await expect(toggle).not.toBeChecked();
  await page.goto("/audit");
  await expect(page.getByText(/switched containers for boxed-/).first()).toBeVisible();
  await expect(page.getByText(/switched off containers for boxed-/).first()).toBeVisible();
});


test("container Orchestrator launch retains isolation when the test launcher is unavailable", async ({ page }) => {
  const { id } = await gitProject(page, `boxed-team-${Date.now()}`);
  await page.goto(`/projects/${id}#project-stack`);  // the containers switch sits in the project's stack settings
  await page.getByRole("checkbox", { name: "Run this project's new sessions in containers" }).check();
  await page.goto('/sessions');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: 'Rename', exact: true }).click();  // the name is suggested until renamed
  await dialog.getByLabel('Name', { exact: true }).fill('Taylor boxed lead');
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(id));
  await openAdvanced(dialog);
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  const created = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/api/sessions'));
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const session = await response.json();
  expect(session.container).toBe(true);
  expect(session.team_id).toBe(session.id);
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session.id}`)).json()).last_error)
    .toContain("the container launcher isn't reachable");
  await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}$`));
  await page.getByRole('tab', { name: 'Setup', exact: true }).click();
  // Setup names the container with its Preview badge (containers-preview-label) and the account's limits (r4).
  await expect(page.getByText("with its account's container limits", { exact: false })).toContainText("Preview");
  await expect(page.getByText('private checkout', { exact: false }).first()).toBeVisible();
  await page.request.post(`/api/sessions/${session.id}/stop`);
});

test("a host that hasn't passed the container qualification can't switch a project on, and says why", async ({ page }, testInfo) => {
  // The real refusal: without this host's record, the server's own check fails (no mocked answer).
  const record = qualificationRecord();
  expect(existsSync(record)).toBe(true);
  renameSync(record, `${record}.away`);
  try {
    const { id, headers } = await gitProject(page, `unqualified-${Date.now()}`);
    const refused = await page.request.post(`/api/projects/${id}/containers`, { headers, data: { enabled: true } });
    expect(refused.status()).toBe(409);
    expect((await refused.json()).detail).toMatch(/^containers are a Preview that needs a qualified host: this host hasn't recorded a passed container qualification/);
    await page.goto(`/projects/${id}#project-stack`);
    const toggle = page.getByRole("checkbox", { name: "Run this project's new sessions in containers" });
    await expect(toggle).toBeDisabled();
    await expect(toggle).toHaveAccessibleDescription(/Containers can't be switched on yet: this host hasn't recorded a passed/);
    await expect(page.getByText("acc.container_host_record record report.json", { exact: false })).toBeVisible();
    await expectAxeClean(page, "main");
    const card = page.locator("#project-stack").locator("section").filter({ has: page.getByRole("heading", { name: "Containers Preview", exact: true }) });
    await card.screenshot({ path: wikiShotPath(testInfo, "projects-containers-unqualified.png") });
  } finally {
    renameSync(`${record}.away`, record);  // the rest of the run keeps a qualified host
  }
  const restored = await (await page.request.get("/api/containers", { headers: await csrfHeaders(page) })).json();
  expect(restored.qualification.passed).toBe(true);
});

for (const width of [1440, 390]) {
  for (const theme of ["dark", "light"] as const) {
    test(`container Preview and lost qualification stay visible at ${width}px in ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme });
      const { id, headers } = await gitProject(page, `preview-${width}-${theme}-${Date.now()}`);
      expect((await page.request.post(`/api/projects/${id}/containers`, { headers, data: { enabled: true } })).ok()).toBe(true);
      await page.goto(`/projects/${id}`);
      await page.getByRole("main").getByRole("button", { name: "New session", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "New session", exact: true });
      await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
      await expect(dialog.getByText("Container session", { exact: false })).toContainText("Preview");
      await expectAxeClean(page, '[role="dialog"]');
      await dialog.screenshot({ path: testInfo.outputPath('container-preview-dialog.png') });
      await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
      const record = qualificationRecord();
      renameSync(record, `${record}.away`);
      try {
        await page.goto(`/projects/${id}#project-stack`);
        await page.reload();  // a hash-only navigation doesn't fetch a new host record
        await expect(page.getByText("New launches, including resumes, are blocked", { exact: false })).toBeVisible();
        await page.locator('#project-stack').screenshot({ path: testInfo.outputPath('container-host-refusal.png') });
        // The existing project's switch stays available: switching off never requires qualification.
        const toggle = page.getByRole("checkbox", { name: "Run this project's new sessions in containers" });
        await expect(toggle).toBeEnabled();
        // Wait for the switch-off itself: a still-on project answers the enable below with 200, not a refusal.
        const switchedOff = page.waitForResponse((r) => r.url().endsWith(`/api/projects/${id}/containers`) && r.request().method() === "POST");
        await toggle.uncheck();
        expect((await switchedOff).ok()).toBe(true);
        await expect(toggle).not.toBeChecked();
        const refused = await page.request.post(`/api/projects/${id}/containers`, { headers, data: { enabled: true } });
        expect(refused.status()).toBe(409);
        const audits = await (await page.request.get('/api/audit')).json();
        expect(audits.some((row: { action: string; target: string }) => row.action === 'project.containers_refused')).toBe(true);
      } finally {
        renameSync(`${record}.away`, record);
      }
    });
  }
}

test('a recorded container session carries Preview in Setup', async ({ page }, testInfo) => {
  const { id, headers } = await gitProject(page, `container-setup-${Date.now()}`);
  // Use a native fake-provider process and render its recorded container flag. No container runtime is installed.
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Setup label' } });
  expect(created.status()).toBe(201);
  const session = await created.json();
  try {
    await page.route(`**/api/sessions/${session.id}`, async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), container: true } });
    });
    await page.goto(`/sessions/${session.id}?tab=spec`);
    await expect(page.getByRole('tabpanel').getByText("A container", { exact: false })).toContainText('Preview');
    await expectAxeClean(page, 'main');
    await page.getByRole('main').screenshot({ path: testInfo.outputPath('container-setup-preview.png') });
  } finally {
    expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers })).ok()).toBe(true);
  }
});
