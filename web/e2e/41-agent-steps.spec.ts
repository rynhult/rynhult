import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

// a card by its heading (a preview agent's heading ends in "preview")
const card = (page: import("@playwright/test").Page, name: string) =>
  page.getByRole("heading", { name: new RegExp(`^${name}( preview)?$`) }).locator("xpath=ancestor::*[self::section or self::article][1]");

// studio-mac: the e2e server's demo Mac (tests/demo_machine.py). Rynhult installs only Codex and Copilot there.
test("a Mac gets step-by-step install instructions, never an Install it would refuse", async ({ page }, testInfo) => {
  await page.goto("/providers?machine=studio-mac");
  // Each provider's software is on its own tile (UX follow-up to W11); the model server is under Other software.
  await expect(card(page, "Codex").getByRole("group", { name: "Codex software" })).toBeVisible();
  const other = card(page, "Other software on studio-mac");
  // restic is a backup tool: not agent software, and studio-mac is no backup target
  await expect(page.getByText("Restic")).toHaveCount(0);
  await expect(page.getByText("Backup tools")).toHaveCount(0);

  for (const name of ["Cursor CLI", "Google Antigravity CLI", "OpenCode"]) {
    const tile = card(page, name);
    await expect(tile.getByRole("button", { name: "Install", exact: true })).toHaveCount(0);
    await expect(tile.getByRole("list", { name: `Install ${name} on studio-mac` })).toBeVisible();
  }
  const ollama = other.getByRole("listitem").filter({ hasText: "Ollama" });
  await expect(ollama.getByRole("button", { name: "Install", exact: true })).toHaveCount(0);
  await expect(ollama.getByText("How to install Ollama on studio-mac")).toBeVisible();
  const copilot = card(page, "GitHub Copilot CLI");
  await expect(copilot.getByRole("button", { name: "Install", exact: true })).toBeVisible();  // this one Rynhult installs on a Mac

  // Claude Code's card: the official Homebrew command, Anthropic's page, Check now, then sign in from the card
  const claude = card(page, "Claude Code");
  await expect(claude.getByRole("button", { name: "Install", exact: true })).toHaveCount(0);
  const steps = claude.getByRole("list", { name: "Install Claude Code on studio-mac" });
  await expect(steps.getByText("brew install --cask claude-code")).toBeVisible();
  await expect(steps.getByText("curl -fsSL https://claude.ai/install.sh | bash")).toBeVisible();
  await expect(steps.getByRole("link", { name: /Claude Code's install instructions on code\.claude\.com/ }))
    .toHaveAttribute("href", "https://code.claude.com/docs/en/setup");
  await expect(steps.getByText("Sign in from this card")).toBeVisible();
  await expect(steps.getByRole("link", { name: "Guide → Connect an agent" })).toHaveAttribute("href", "/guide/User-Guide/Connect-an-Agent");
  await expect(steps.getByRole("button", { name: "Copy the Claude Code install command", exact: true })).toBeVisible();
  await expectAxeClean(page, "main");
  await claude.scrollIntoViewIfNeeded();
  await claude.screenshot({ path: wikiShotPath(testInfo, "connect-agent-mac.png") });

  // Cursor was installed by hand on the Mac: Check now finds it and the steps give way to its sign-in
  const cursor = card(page, "Cursor CLI");
  await expect(cursor.getByText("curl https://cursor.com/install -fsS | bash")).toBeVisible();
  await cursor.getByRole("button", { name: "Check now for Cursor CLI on studio-mac" }).click();
  const row = cursor.getByRole("group", { name: "Cursor CLI software" });
  await expect(row.getByText("2026.10.17", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(row.getByText("installed by hand")).toBeVisible();
  await expect(row.getByRole("button", { name: /Remove/ })).toHaveCount(0);
  await expect(cursor.getByRole("list", { name: /Install Cursor CLI/ })).toHaveCount(0);
  await expect(page.getByText(/refused a request/)).toHaveCount(0);

  // the API refuses what the dashboard no longer offers
  const refused = await page.request.post("/api/packages/ollama/install?machine=studio-mac", { headers: await csrfHeaders(page) });
  expect(refused.status()).toBe(409);
  expect((await refused.json()).detail).toMatch(/^Ollama can't be installed on a Mac from Rynhult/);
});

test("the main Rynhult lists restic under Backup tools, apart from the agent CLIs", async ({ page }) => {
  await page.goto("/providers");
  const other = card(page, "Other software");
  await expect(other.getByRole("heading", { name: "Backup tools" })).toBeVisible();
  await expect(other.getByRole("list", { name: "Backup tools" }).getByText("Restic")).toBeVisible();
  await expect(page.locator("#provider-sign-ins article").getByText("Restic")).toHaveCount(0);
});
