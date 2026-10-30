import { wikiShotPath } from "./helpers";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { AUTH_FILE, info, fileRow } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("Files: browse, Markdown preview, save, personal commit, conflict and phone layout", async ({ page }, testInfo) => {
  const root = `${info().workspace}/files-demo`;
  mkdirSync(root, { recursive: true });
  execFileSync("git", ["init", "-q", "-b", "main", root]);
  writeFileSync(`${root}/notes.md`, "# Original\n\nA demo document.\n");
  writeFileSync(`${root}/other.py`, 'def greet():\n    return "Hello"\n' );
  writeFileSync(`${root}/.env`, "EXAMPLE=protected");
  execFileSync("git", ["-C", root, "add", "notes.md"]);
  execFileSync("git", ["-C", root, "-c", "user.name=Alex Example", "-c", "user.email=alex@example.com", "commit", "-qm", "Start"]);
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const project = await page.request.post("/api/projects", { data: { slug: "files-demo", name: "Files demo", workspace_path: root }, headers: { "x-acc-csrf": csrf } });
  expect(project.status()).toBe(201);
  const id = (await project.json()).id;
  await page.goto(`/projects/${id}?tab=files`);
  await page.getByRole("button", { name: fileRow("notes.md") }).click();
  await expect(page.getByRole("heading", { name: "Original", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: fileRow(".env") })).toBeDisabled();
  const editor = page.getByRole("textbox", { name: "File content" });
  await editor.fill("# Updated\n\nEdited in Rynhult.");
  await expect(page.getByRole("heading", { name: "Updated", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Save", exact: true }).dblclick();
  await expect(page.getByRole("status", { name: "File save status" })).toContainText("previous copy is kept as");
  const saved = await page.getByRole("status", { name: "File save status" }).innerText();
  const previous = saved.match(/\.rynhult-replaced-[0-9]+-[a-f0-9]+/)![0];
  expect(readFileSync(`${root}/${previous}`, "utf8")).toContain("Original");
  expect(readFileSync(`${root}/notes.md`, "utf8")).toContain("Edited in Rynhult.");
  await page.getByRole("button", { name: fileRow("other.py") }).click();
  await expect(editor).toContainText("Hello");
  const codeAccessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(codeAccessibility.violations).toEqual([]);
  await page.getByRole("button", { name: fileRow("notes.md") }).click();
  await expect(editor).toContainText("Edited in Rynhult.");
  await page.getByRole("button", { name: "Commit identity" }).click();
  await page.getByLabel("Commit name").fill("Alex Example");
  await page.getByLabel("Commit email").fill("alex@example.com");
  await page.getByRole("button", { name: "Save identity" }).dblclick();
  await editor.fill("# Committed\n\nLocal history.");
  await page.getByLabel("Commit message").fill("Update demo notes");
  await page.getByRole("button", { name: "Save and commit", exact: true }).dblclick();
  await expect(page.getByRole("status", { name: "File save status" })).toContainText("Committed");
  const log = execFileSync("git", ["-C", root, "log", "-1", "--format=%an <%ae> %s"]).toString();
  expect(log.trim()).toBe("Alex Example <alex@example.com> Update demo notes");
  await editor.fill("# My draft\n\nKeep this version.");
  writeFileSync(`${root}/notes.md`, "# Agent edit\n\nConcurrent version.");
  await page.getByRole("button", { name: "Save", exact: true }).dblclick();
  await expect(page.getByRole("heading", { name: "File changed on disk" })).toBeVisible();
  await expect(page.getByLabel("Current version on disk")).toContainText("Concurrent version.");
  await expect(page.getByRole("region", { name: "Current version on disk" })).toHaveAttribute("tabindex", "0");
  await expect(editor).toContainText("Keep this version.");
  expect(readFileSync(`${root}/notes.md`, "utf8")).toContain("Concurrent version.");
  await page.getByRole("button", { name: "Keep draft and use current revision" }).click();
  await page.getByRole("button", { name: "Save", exact: true }).dblclick();
  await expect(page.getByRole("status", { name: "File save status" })).toContainText("previous copy is kept as");
  expect(readFileSync(`${root}/notes.md`, "utf8")).toContain("Keep this version.");
  await editor.fill("# Files demo\n\nEdit Markdown with a live preview. Saves stay in the local repository.");
  await page.getByRole("button", { name: "Save", exact: true }).dblclick();
  await expect(page.getByRole("status", { name: "File save status" })).toContainText("previous copy is kept as");
  const accessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(accessibility.violations).toEqual([]);
  await page.getByRole("button", { name: "New folder", exact: true }).click();
  await page.getByLabel("Path", { exact: true }).fill("docs");
  await page.getByRole("button", { name: "Create", exact: true }).dblclick();
  await page.getByRole("button", { name: "New file", exact: true }).click();
  await page.getByLabel("Path", { exact: true }).fill("docs/demo.txt");
  await page.getByRole("button", { name: "Create", exact: true }).dblclick();
  await page.getByRole("button", { name: fileRow("docs", "Toggle") }).click();
  await page.getByRole("button", { name: "Manage docs/demo.txt", exact: true }).click();
  await page.getByLabel("Path", { exact: true }).fill("docs/renamed.txt");
  await page.getByRole("button", { name: "Rename", exact: true }).dblclick();
  await page.getByRole("button", { name: "Rename", exact: true }).dblclick();
  await expect(page.getByRole("button", { name: fileRow("docs/renamed.txt") })).toBeVisible();
  await page.getByRole("button", { name: "Manage docs/renamed.txt", exact: true }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).dblclick();
  await page.getByRole("button", { name: "Delete", exact: true }).dblclick();
  await expect(page.getByRole("button", { name: fileRow("docs/renamed.txt") })).toHaveCount(0);
  await page.screenshot({ path: wikiShotPath(testInfo, "files-editor.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Show tree" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await expect(editor).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "files-phone.png"), fullPage: true });
});

test("Files opens the session and workspace folder", async ({ page }, testInfo) => {
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const launched = await page.request.post("/api/sessions", { headers: { "x-acc-csrf": csrf }, data: { topic: "Files workspace", name: "Files workspace", profile: "researcher", provider: "claude" } });
  expect(launched.status(), await launched.text()).toBe(201);
  const session = await launched.json();
  try {
    const created = await page.request.post(`/api/files/session/${session.id}`, { headers: { "x-acc-csrf": csrf }, data: { action: "create", path: "workspace.md", content: "# Workspace folder" } });
    expect(created.status()).toBe(200);
    await page.goto(`/sessions/${session.id}?tab=files`);
    await page.getByRole("button", { name: fileRow("workspace.md") }).click();
    await expect(page.getByRole("heading", { name: "Workspace folder", exact: true })).toBeVisible();
    await expect(page.getByText(/A session is working in this folder/)).toBeVisible();
    await page.goto("/workspaces");
    await page.getByRole("button", { name: "Workspace actions for Files workspace", exact: true }).click();
    await page.locator(`a[href$="files?session=${session.id}"]`).click();
    await expect(page.getByRole("tab", { name: "Files", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: fileRow("workspace.md") }).click();
    await expect(page.getByRole("heading", { name: "Workspace folder", exact: true })).toBeVisible();
  } finally {
    await page.request.post(`/api/sessions/${session.id}/stop`, { headers: { "x-acc-csrf": csrf } });
  }
});

test("Files downloads keep their name and safe media uses native players", async ({ page }) => {
  const root = `${info().workspace}/files-download`;
  mkdirSync(root, { recursive: true });
  writeFileSync(`${root}/résumé notes.txt`, "A downloadable document.\n");
  // Small binary headers exercise the broker's media recognition without external codecs/providers.
  writeFileSync(`${root}/sample.mp3`, Buffer.from([0x49, 0x44, 0x33, 4, 0, 0, 0, 0, 0, 0]));
  writeFileSync(`${root}/sample.mp4`, Buffer.from([0, 0, 0, 24, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d, 0, 0, 0, 0]));
  writeFileSync(`${root}/unsafe.svg`, '<svg xmlns="http://www.w3.org/2000/svg"/>');
  execFileSync("git", ["init", "-q", "-b", "main", root]);
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "-c", "user.name=Alex Example", "-c", "user.email=alex@example.com", "commit", "-qm", "Start"]);
  await page.goto("/");
  const csrf = (await page.context().cookies()).find(c => /acc_csrf$/.test(c.name))!.value;
  const response = await page.request.post("/api/projects", { headers: { "x-acc-csrf": csrf },
    data: { slug: "files-download", name: "Files download", workspace_path: root } });
  expect(response.status()).toBe(201);
  const project = await response.json();
  expect((await page.request.post(`/api/projects/${project.id}/trust`, { headers: { "x-acc-csrf": csrf } })).status()).toBe(200);
  const launched = await page.request.post("/api/sessions", { headers: { "x-acc-csrf": csrf },
    data: { project_id: project.id, provider: "claude", name: "Download session" } });
  expect(launched.status(), await launched.text()).toBe(201);
  const session = await launched.json();
  try {
    await page.goto(`/sessions/${session.id}?tab=files`);
    await page.getByRole("button", { name: fileRow("résumé notes.txt") }).click();
    const downloading = page.waitForEvent("download");
    await page.getByRole("link", { name: "Download", exact: true }).click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe("résumé notes.txt");
    expect(readFileSync((await download.path())!, "utf8")).toBe("A downloadable document.\n");
    await page.getByRole("button", { name: fileRow("sample.mp3") }).click();
    const audio = page.locator("audio");
    await expect(audio).toBeVisible();
    await expect(audio).toHaveAttribute("controls", "");
    await expect(audio).toHaveAttribute("src", `/api/files/session/${session.id}/download?path=sample.mp3`);
    await expect(page.getByRole("img", { name: "sample.mp3", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: fileRow("sample.mp4") }).click();
    await expect(page.locator("video")).toBeVisible();
    await expect(page.locator("video")).toHaveAttribute("controls", "");
    await expect(page.locator("audio")).toHaveCount(0);
    await page.getByRole("button", { name: fileRow("unsafe.svg") }).click();
    await expect(page.locator("audio, video, iframe, object, embed")).toHaveCount(0);
    await expect(page.getByRole("img", { name: "unsafe.svg", exact: true })).toHaveCount(0);
  } finally {
    await page.request.post(`/api/sessions/${session.id}/stop`, { headers: { "x-acc-csrf": csrf } });
  }
});
