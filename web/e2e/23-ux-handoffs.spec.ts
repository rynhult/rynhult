import { expect, test } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { AUTH_FILE, expectAxeClean, gitProject } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review SE-2 and SE-3: the Handoffs tab reads the session's own folder, and never says "none" when it couldn't read.
test("handoffs: a worktree session's note appears and opens; a shared-folder session's too", async ({ page }) => {
  const { id, folder, headers } = await gitProject(page, "handoff-demo");
  const launch = async (name: string, worktree: boolean) =>
    (await (await page.request.post("/api/sessions", { headers, data: { project_id: id, provider: "claude", name, worktree } })).json()) as { id: string; worktree: string | null };
  const tree = await launch("Tree notes", true);
  const shared = await launch("Shared notes", false);
  expect(tree.worktree).toBeTruthy();
  mkdirSync(`${tree.worktree}/docs/handoffs`, { recursive: true });
  writeFileSync(`${tree.worktree}/docs/handoffs/2026-10-22-${tree.id}-checkout.md`, "# Checkout\n\nOne-page checkout works.\n");
  mkdirSync(`${folder}/docs/handoffs`, { recursive: true });
  writeFileSync(`${folder}/docs/handoffs/2026-10-22-${shared.id}-notes.md`, "# Notes\n\nWritten in the shared checkout.\n");

  await page.goto(`/sessions/${tree.id}?tab=handoffs`);
  const note = page.getByRole("button", { name: new RegExp(`2026-10-22-${tree.id}-checkout.md`) });
  await expect(note).toBeVisible();
  await expect(page.getByText("No handoff note yet")).toHaveCount(0);
  await expectAxeClean(page, "main");
  await note.click();
  const viewer = page.getByRole("dialog", { name: `docs/handoffs/2026-10-22-${tree.id}-checkout.md` });
  await expect(viewer).toContainText("One-page checkout works.");
  await page.keyboard.press("Escape");

  await page.goto(`/sessions/${shared.id}?tab=handoffs`);
  await expect(page.getByRole("button", { name: new RegExp(`${shared.id}-notes.md`) })).toBeVisible();
});

test("handoffs: a failed read shows an error with Retry, never 'no handoff'", async ({ page }) => {
  const { id, headers } = await gitProject(page, "handoff-fail");
  const session = await (await page.request.post("/api/sessions", { headers, data: { project_id: id, provider: "claude", name: "Unreadable" } })).json();
  let fail = true;
  await page.route(`**/api/sessions/${session.id}/knowledge`, (route) => fail
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "broker unavailable" }) })
    : route.continue());
  await page.goto(`/sessions/${session.id}?tab=handoffs`);
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Couldn't read this session's handoff notes.");
  await expect(page.getByText("No handoff note yet")).toHaveCount(0);
  await expectAxeClean(page, "main");
  fail = false;
  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("No handoff note yet")).toBeVisible();  // only once the read succeeded
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("handoffs: while the read is in flight, a skeleton shows and not the empty text", async ({ page }) => {
  const { id, headers } = await gitProject(page, "handoff-slow");
  const session = await (await page.request.post("/api/sessions", { headers, data: { project_id: id, provider: "claude", name: "Slow" } })).json();
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/api/sessions/${session.id}/knowledge`, async (route) => { await held; await route.continue(); });
  await page.goto(`/sessions/${session.id}?tab=handoffs`);
  await expect(page.getByRole("status", { name: "Loading" })).toBeVisible();
  await expect(page.getByText("No handoff note yet")).toHaveCount(0);
  release();
  await expect(page.getByText("No handoff note yet")).toBeVisible();
});
