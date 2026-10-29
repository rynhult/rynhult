import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Orchestrator v2 slice 5: the gate queue is off until an owner turns it on; owners set the commands a gate runs.
test("owners set gate commands and the host budget, then queue and cancel a gate on the project page", async ({ page }, testInfo) => {
  const { id, folder } = await gitProject(page, `gated-${Date.now()}`);
  execFileSync("git", ["-C", folder, "branch", "acc/team-demo"]);
  try {
    await page.goto(`/projects/${id}#project-gates`);
    // The card itself, not the Project settings section around it.
    const card = page.locator("section:not(#project-settings)").filter({ has: page.getByRole("heading", { name: "Gate queue" }) });
    await expect(card.getByText("The gate queue is off. Turn it on below.")).toBeVisible();
    await expect(card.getByRole("form", { name: "Queue a gate" })).toHaveCount(0);

    const commands = card.getByRole("form", { name: "Gate commands" });
    await commands.getByLabel("Full suite").fill("python3\n-m\npytest\n--junitxml={junit}");
    // A related run that takes a while, so the cancel below stops a real running process through its record.
    await commands.getByLabel("Related tests").fill("python3\n-c\nimport time; time.sleep(120)\n{junit}");
    await commands.getByRole("button", { name: "Save commands" }).click();
    await expect(commands.getByRole("button", { name: "Save commands" })).toBeEnabled();

    const budget = card.getByRole("form", { name: "Host test budget" });
    await expect(budget.getByText("Orchestrator v2 is on by default. Gates stay off until an owner enables this host budget and accepts the branch-code trust warning.")).toBeVisible();
    await expect(budget.getByLabel("Gate slots")).toHaveValue("2");          // the owner's defaults for this host
    await expect(budget.getByLabel("Full suites at once")).toHaveValue("1");
    await expect(budget.getByLabel("Load ceiling")).toHaveValue("16");
    await budget.getByLabel("Run the gate queue").check();
    await budget.getByRole("button", { name: "Save budget" }).click();
    // Gates trust branch code (review L-1): turning the queue on says so first, and Cancel saves nothing.
    const trust = page.getByRole("dialog", { name: "Turn on the gate queue? Gates trust branch code" });
    await expect(trust).toContainText("outside the helpers' sandbox, permission mode and guardrails");
    await expectAxeClean(page, "[role=dialog]");
    await trust.getByRole("button", { name: "Cancel" }).click();
    expect((await (await page.request.get("/api/gates/budget")).json()).enabled).toBe(false);
    await budget.getByRole("button", { name: "Save budget" }).click();
    await page.getByRole("dialog", { name: "Turn on the gate queue? Gates trust branch code" })
      .getByRole("button", { name: "Turn on the gate queue" }).click();
    await expect(card.getByText("Slots 0/2 · full suites 0/1 · load ceiling 16")).toBeVisible();

    const queue = card.getByRole("form", { name: "Queue a gate" });
    await queue.getByLabel("Branch").fill("acc/team-demo");
    await queue.getByLabel("Priority").selectOption("merge");
    await queue.getByRole("button", { name: "Queue gate" }).click();
    const job = card.getByRole("list", { name: "Gate jobs" }).getByRole("listitem").filter({ hasText: "acc/team-demo" });
    await expect(job.getByText("merge", { exact: true })).toBeVisible();
    await expect(job.getByText("running", { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(card.getByText("Slots 1/2 · full suites 0/1 · load ceiling 16")).toBeVisible();
    await card.screenshot({ path: wikiShotPath(testInfo, "gates-project.png") });
    await expectAxeClean(page, "main");

    await job.getByRole("button", { name: "Cancel" }).click();
    const dialog = page.getByRole("dialog", { name: "Cancel the gate for acc/team-demo?" });
    await dialog.getByRole("button", { name: "Cancel gate" }).click();
    await expect(job.getByText("cancelled", { exact: true })).toBeVisible({ timeout: 20_000 });  // the card refetches every 5 s
  } finally {
    // The host budget is installation-wide: leave it as the other specs expect (off).
    await page.request.put("/api/gates/budget", { headers: await csrfHeaders(page), data: { enabled: false } });
  }
});
