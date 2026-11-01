import { wikiShotPath } from "./helpers";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { appendFileSync } from "node:fs";
import { AUTH_FILE, info } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("tool requests show destination, approve pins version, reject does not install, removal is approved", async ({ page }, testInfo) => {
  await page.goto("/providers");
  const csrf = (await page.context().cookies()).find(c => c.name === "acc_csrf")!.value;
  const headers = { "X-ACC-CSRF": csrf, Origin: info().origin };
  // Own the session: the newest running session may belong to a demoted member
  // whose account no longer matches it, so tool requests from it are refused.
  const project = await page.request.post("/api/projects", { headers,
    data: { slug: `tools-${Date.now()}`, name: "Tools demo", workspace_path: info().workspace, host: "local" } });
  expect(project.ok()).toBe(true);
  const pid = (await project.json()).id;
  const trust = await page.request.post(`/api/projects/${pid}/trust`, { headers });
  expect(trust.ok()).toBe(true);
  const session = await page.request.post("/api/sessions", { headers,
    data: { project_id: pid, provider: "claude", name: "Tools demo" } });
  expect(session.ok()).toBe(true);
  const sid = (await session.json()).id;
  try {
    const request = (name: string, manager = "npm") => appendFileSync(`${info().run_dir}/${sid}/events.jsonl`, JSON.stringify({
      provider: "acc", hook: { hook_event_name: "AccRequest", request: {
        type: "tool", manager, name, reason: "Format the demo output", action: "install",
      } },
    }) + "\n");
    request("prettier");
    await page.goto("/inbox");
    const row = page.getByRole("region", { name: "Tool request: prettier" });
    await expect(row).toContainText("npm");
    await expect(row).toContainText("agent account");
    await expect(row).toContainText("Format the demo output");
    await page.screenshot({ path: wikiShotPath(testInfo, "tools-inbox.png"), fullPage: true });
    await row.getByRole("button", { name: "Approve", exact: true }).dblclick();
    await page.goto("/providers");
    const tools = page.getByRole("region", { name: "Tools", exact: true });
    await expect(tools).toContainText("prettier");
    await expect(tools).toContainText("3.6.2");
    await expect(tools).toContainText("local");
    await tools.screenshot({ path: wikiShotPath(testInfo, "tools-list.png") });
    const axe = await new AxeBuilder({ page }).include('[aria-label="Tools"]').analyze();
    expect(axe.violations).toEqual([]);
    await page.setViewportSize({ width: 390, height: 844 });
    await tools.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await tools.screenshot({ path: wikiShotPath(testInfo, "tools-list-phone.png") });
    await page.setViewportSize({ width: 1440, height: 900 });

    request("typescript");
    await page.goto("/inbox");
    await page.getByRole("region", { name: "Tool request: typescript" }).getByRole("button", { name: "Reject", exact: true }).dblclick();
    await page.goto("/providers");
    await tools.locator("summary").click();
    await expect(tools.locator("details").getByRole("listitem").filter({ hasText: "install typescript" }))
      .toContainText("rejected");
    await expect(tools.getByRole("button", { name: "Remove typescript", exact: true })).toHaveCount(0);
    await tools.getByRole("button", { name: "Remove prettier" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Request removal" }).dblclick();
    await expect(tools).toContainText("prettier");
    await page.goto("/inbox");
    await page.getByRole("region", { name: "Tool request: prettier" }).getByRole("button", { name: "Approve", exact: true }).dblclick();
    await page.goto("/providers");
    await expect(tools).toContainText("No tools installed through Rynhult");

    await page.goto("/settings#tool-approvals");
    const policy = page.locator("section").filter({ has: page.getByRole("heading", { name: "Tool approvals", exact: true }) });
    // The legacy unreviewed writer is retired. Requests still require owner approval.
    await expect(policy.getByRole('checkbox')).toHaveCount(0);
    await expect(policy.getByRole('link', { name: 'Open reviewed Security settings' })).toHaveAttribute('href', '/settings#security');
    await policy.screenshot({ path: wikiShotPath(testInfo, "tools-policy.png") });

    request("ripgrep", "apt");
    await page.goto("/inbox");
    const system = page.getByRole("region", { name: "Tool request: ripgrep" });
    await expect(system).toContainText("system-wide");
    await system.getByRole("button", { name: "Approve", exact: true }).dblclick();
    await page.goto("/providers");
    await expect(tools).toContainText("ripgrep");
    await page.getByRole("radiogroup", { name: "Agent account" }).getByRole("radio", { name: "alex-agent", exact: true }).click();
    await expect(tools).toContainText("ripgrep");
    await tools.getByRole("button", { name: "Remove ripgrep" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Request removal" }).dblclick();
    await expect(tools.getByRole("button", { name: "Change waiting for ripgrep" })).toBeDisabled();  // its name follows its text
  } finally {
    const stopped = await page.request.post(`/api/sessions/${sid}/stop`, { headers });
    expect(stopped.ok()).toBe(true);
  }
});
