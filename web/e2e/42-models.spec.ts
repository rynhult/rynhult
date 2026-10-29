import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Owner: "make these collapse, cursor's list is incredibly long". A long list (40) and a short one (3).
const long = Array.from({ length: 40 }, (_, i) => ({ id: `model-${i + 1}`, label: `Model ${i + 1}${i === 7 ? " Sonnet" : ""}` }));
const models = { fetched_at: Date.now() / 1000, codex: long,
  claude: [{ id: "opus", label: "Opus" }, { id: "sonnet", label: "Sonnet" }, { id: "haiku", label: "Haiku" }] };

test.beforeEach(async ({ page }) => {
  await page.route("**/api/models", (route) => route.fulfill({ json: models }));
  await page.addInitScript(() => { if (!sessionStorage.getItem("kept")) { localStorage.clear(); sessionStorage.setItem("kept", "1"); } });
});

test("available models: long lists collapse, remember their state, and one filter narrows them all", async ({ page }) => {
  await page.goto("/providers#provider-models");
  const codex = page.getByRole("region", { name: /^Codex/ });
  const claude = page.getByRole("region", { name: /^Claude Code/ });
  await expect(codex.getByRole("list", { name: "Codex models" }).getByRole("listitem")).toHaveCount(6);
  await expect(claude.getByRole("listitem")).toHaveCount(3);
  await expect(claude.getByRole("button", { name: /Show all/ })).toHaveCount(0);  // short: nothing to collapse
  const more = codex.getByRole("button", { name: "Show all 40 Codex models" });
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await more.focus();
  await page.keyboard.press("Enter");  // keyboard works
  await expect(codex.getByRole("listitem")).toHaveCount(40);
  const fewer = codex.getByRole("button", { name: "Show the first 6 Codex models" });
  await expect(fewer).toHaveAttribute("aria-expanded", "true");
  await expect(fewer).toHaveAttribute("aria-controls", await codex.getByRole("list").getAttribute("id") ?? "");
  await expectAxeClean(page, "main");

  await page.reload();  // remembered for this person in this browser
  await expect(page.getByRole("region", { name: /^Codex/ }).getByRole("listitem")).toHaveCount(40);

  const filter = page.getByRole("searchbox", { name: "Filter models" });
  await filter.fill("sonnet");  // across groups: one in each
  await expect(page.getByRole("region", { name: /^Codex/ }).getByRole("listitem")).toHaveCount(1);
  await expect(page.getByRole("region", { name: /^Claude Code/ }).getByRole("listitem")).toHaveCount(1);
  await expect(page.getByText("1 of 40 models")).toBeVisible();
  await filter.fill("model-12");
  await expect(page.getByRole("region", { name: /^Claude Code/ })).toHaveCount(0);  // no match: the group goes
  await filter.fill("nothing like this");
  await expect(page.getByText("No models match “nothing like this”.")).toBeVisible();
  await filter.fill("");
  await page.getByRole("button", { name: "Show the first 6 Codex models" }).click();
  await expect(page.getByRole("region", { name: /^Codex/ }).getByRole("listitem")).toHaveCount(6);
});

test("available models fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/providers#provider-models");
  await page.getByRole("searchbox", { name: "Filter models" }).scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "Show all 40 Codex models" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
