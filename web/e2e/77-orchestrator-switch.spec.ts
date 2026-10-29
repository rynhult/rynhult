import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE, gitProject, openAdvanced } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Orchestrator v2 is on by default; explicit Off hides its entries.
// with it off, /api/auth/state says so and every v2 entry is absent: the Settings nav entry, its section and the
// project's Gate queue card (the server then has no v2 routes either: tests/test_orchestrator_switch.py).
async function orchestratorOff(page: Page) {
  await page.route("**/api/auth/state", async (route) => {
    const response = await route.fetch();
    const state = await response.json();
    await route.fulfill({ response, json: { ...state, instance: { ...state.instance, orchestrator: false } } });
  });
}

test("Orchestrator entries show by default and hide when the server turns it off", async ({ page }) => {
  const { id } = await gitProject(page, `orchestrator-${Date.now()}`);
  const nav = page.getByRole("navigation", { name: "Settings sections" });
  const gateCard = page.getByRole("heading", { name: "Gate queue" });

  await page.goto("/settings#all");  // every section and the full list of links
  await expect(nav.getByRole("link", { name: "Selection allow-lists" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Selection allow-lists" })).toBeVisible();
  await page.goto(`/projects/${id}#project-gates`);
  await expect(page.getByRole("tab", { name: "Gate queue", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(gateCard).toBeVisible();

  await orchestratorOff(page);
  await page.goto("/settings#all");  // every section and the full list of links
  await expect(nav.getByRole("link", { name: "Budgets" })).toBeVisible();  // the nav itself is there
  await expect(nav.getByRole("link", { name: "Selection allow-lists" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Selection allow-lists" })).toHaveCount(0);
  await page.goto(`/projects/${id}#project-ports`);
  await expect(page.getByRole("tab", { name: "Gate queue", exact: true })).toHaveCount(0);
  await expect(page.locator("#project-ports")).toBeVisible();  // the settings beside it have loaded
  await expect(gateCard).toHaveCount(0);
});


for (const width of [1440, 390]) for (const theme of ["dark", "light"] as const) {
  test(`gate queue deep link and keyboard navigation at ${width} ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    const { id } = await gitProject(page, `gate-tab-${width}-${theme}-${Date.now()}`);
    await page.goto(`/projects/${id}#project-gates`);
    const tab = page.getByRole("tab", { name: "Gate queue", exact: true });
    await expect(tab).toHaveAttribute("aria-selected", "true");
    await expect(tab).toBeFocused();
    await expect(page.getByRole("heading", { name: "Gate queue" })).toBeVisible();
    await tab.press("ArrowRight");
    const next = page.getByRole("tab", { name: "Project brief", exact: true });
    await expect(next).toBeFocused();
    await expect(next).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#project-gates")).toBeHidden();
    await next.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Knowledge", exact: true })).toBeFocused();
  });
}


test("New session offers helpers as a normal feature", async ({ page }) => {
  const { id } = await gitProject(page, `helpers-default-${Date.now()}`);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
  await openAdvanced(dialog);
  const helpers = dialog.getByRole("radio", { name: /^With helpers/ });
  await expect(helpers).toHaveAccessibleName("With helpers This session may start a team of helper agents within limits you set.");
  await helpers.check();
  await expect(dialog.getByRole("group", { name: "Team limits", exact: true })).toBeVisible();
  await expect(dialog.getByRole("group", { name: "Run", exact: true }).getByText(/preview/i)).toHaveCount(0);
  await expect(dialog.getByRole("group", { name: "Team limits", exact: true }).getByText(/preview/i)).toHaveCount(0);
  await page.keyboard.press("Escape");
});
