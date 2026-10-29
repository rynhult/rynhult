import { wikiShotPath } from "./helpers";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { AUTH_FILE, info } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("explain explicitly launches one helper, navigates steps and reuses the revision", async ({ page }, testInfo) => {
  const slug = `explain-demo-${randomUUID().slice(0, 8)}`;
  const workspace = `${info().workspace}/${slug}`;
  mkdirSync(workspace, { recursive: true });
  execFileSync("git", ["init", "-q", "-b", "main", workspace]);
  writeFileSync(`${workspace}/app.py`, "one\ntwo\n");
  execFileSync("git", ["-C", workspace, "add", "."]);
  execFileSync("git", ["-C", workspace, "-c", "user.name=Alex Example",
    "-c", "user.email=alex@example.com", "commit", "-qm", "Initial version"]);
  await page.goto("/sessions");
  const csrf = (await page.context().cookies()).find(c => c.name === "acc_csrf")!.value;
  const headers = { "X-ACC-CSRF": csrf, Origin: info().origin };
  const project = await page.request.post("/api/projects", { headers,
    data: { slug, name: "Demo walkthrough", workspace_path: workspace } });
  expect(project.status()).toBe(201);
  const pid = (await project.json()).id;
  await page.request.post(`/api/projects/${pid}/trust`, { headers });
  const launched = await page.request.post("/api/sessions", { headers,
    data: { project_id: pid, provider: "claude", name: "Explain demo" } });
  expect(launched.status()).toBe(201);
  const sid = (await launched.json()).id;
  const helpers = async () => {
    const audit = await (await page.request.get("/api/audit?action=session.explain")).json();
    const ids = new Set(audit.filter((event: { target: string }) => event.target === sid)
      .map((event: { detail: { helper_id: string } }) => event.detail.helper_id));
    return (await (await page.request.get("/api/sessions")).json())
      .filter((session: { id: string; name: string }) => ids.has(session.id) && session.name === "Explain these changes");
  };
  const generateWalkthrough = async () => {
    const path = `/api/sessions/${sid}/explanation`;
    const previous = await page.request.get(path);
    expect(previous.ok()).toBe(true);
    const previousId = (await previous.json()).helper_id;
    const completed = page.waitForResponse(async response => {
      if (new URL(response.url()).pathname !== path || response.request().method() !== "GET") return false;
      if (!response.ok()) return true;
      const result = await response.json();
      return result.helper_id !== previousId && ["ready", "error"].includes(result.status);
    });
    await page.getByRole("button", { name: "Generate walkthrough" }).click();
    const response = await completed;
    expect(response.ok()).toBe(true);
    expect(await response.json()).toMatchObject({ status: "ready" });
  };
  try {
    writeFileSync(`${workspace}/app.py`, "one\nTWO\n");
    await page.goto(`/sessions/${sid}?tab=changes`);
    await page.getByRole("tab", { name: /Changes/ }).click();
    const before = (await helpers()).length;
    await page.getByRole("button", { name: "Explain these changes", exact: true }).click();
    await expect(page.getByRole("button", { name: "Generate walkthrough" })).toBeVisible();
    expect((await helpers()).length).toBe(before);
    await page.getByLabel("Explanation model").fill("sonnet");
    await generateWalkthrough();
    const panel = page.getByRole("complementary", { name: "Change walkthrough" });
    await expect(panel.getByText("Step 1 of 2")).toBeVisible();
    await expect(panel.getByText("generated; check the diff")).toBeVisible();
    await expect(panel.getByText(/sonnet/)).toBeVisible();
    expect((await helpers()).length).toBe(before + 1);
    await panel.getByRole("link", { name: "app.py:2–2 (new)" }).click();
    await expect(page.locator('[data-explanation-highlight="true"]')).toContainText("+TWO");
    await panel.getByRole("button", { name: "Next step" }).click();
    await expect(panel.getByRole("heading", { name: "Check the impact" })).toBeVisible();
    await panel.getByRole("button", { name: "Previous step" }).click();
    await expect(panel.getByText("Step 1 of 2")).toBeVisible();
    expect((await new AxeBuilder({ page }).include('aside[aria-label="Change walkthrough"]').analyze()).violations).toEqual([]);
    await page.screenshot({ path: wikiShotPath(testInfo, "sessions-explain-changes.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(panel).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: wikiShotPath(testInfo, "sessions-explain-changes-phone.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.reload();
    await page.getByRole("tab", { name: /Changes/ }).click();
    await page.getByRole("button", { name: "Explain these changes", exact: true }).click();
    await expect(panel.getByText("Step 1 of 2")).toBeVisible();
    expect((await helpers()).length).toBe(before + 1);
    await panel.getByRole("button", { name: "Regenerate" }).click();
    await expect(panel.getByRole("button", { name: "Generate walkthrough" })).toBeVisible();
    await generateWalkthrough();
    await expect(panel.getByText("Step 1 of 2")).toBeVisible();
    expect((await helpers()).length).toBe(before + 2);
  } finally {
    for (const id of [sid, ...(await helpers()).map((session: { id: string }) => session.id)]) {
      expect((await page.request.post(`/api/sessions/${id}/stop`, { headers })).ok()).toBe(true);
    }
  }
});
