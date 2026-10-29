import { expect, test } from "@playwright/test";
import { AUTH_FILE, sessionWithChanges } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review CO-1: words, not engine identifiers, on Activity, Setup, Audit and Catalog; raw values stay in tooltips.
test("Activity, Setup, Audit and Catalog speak in words", async ({ page }) => {
  const id = await sessionWithChanges(page, "words-demo");
  const session = await (await page.request.get(`/api/sessions/${id}`)).json();
  await page.goto(`/sessions/${id}?tab=activity`);
  await expect(page.getByText("Started", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("run_launch")).toHaveCount(0);
  await expect(page.getByText("Started", { exact: true }).first()).toHaveAttribute("title", "run_launch");

  await page.getByRole("tab", { name: "Setup" }).click();
  await expect(page.getByRole("heading", { name: "How this session was set up" })).toBeVisible();
  await expect(page.getByText("Accept edits").first()).toBeVisible();

  // Viewing the terminal twice writes two attaches; Audit shows one sentence for them.
  await page.goto(`/sessions/${id}`);
  await expect(page.locator(".xterm-helper-textarea")).toBeFocused();
  await page.reload();
  await expect(page.locator(".xterm-helper-textarea")).toBeFocused();
  await page.goto("/audit");
  await page.getByRole("combobox", { name: "Type" }).selectOption({ label: "Terminals" });
  const row = page.getByText(`opened ${session.name}'s terminal to type`, { exact: false });
  await expect(row).toHaveCount(1);
  await expect(page.getByText(/times that day/).first()).toBeVisible();
  await expect(page.getByText("terminal.attach")).toHaveCount(0);

  await page.goto("/catalog");
  await expect(page.getByRole("tab", { name: /Subagents/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Not loaded by default/ })).toBeVisible();
  await expect(page.getByText(/Codex: Can edit the project/).first()).toBeVisible();
  await expect(page.getByText(/revision [0-9a-f]{8}/)).toHaveCount(0);
});
