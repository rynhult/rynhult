import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { AUTH_FILE, info, fileRow, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`Files uploads: button, folder drop and conflict choices at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const root = `${info().workspace}/uploads-${width}`;
    mkdirSync(`${root}/images`, { recursive: true });
    mkdirSync(`${root}/.ssh`, { recursive: true });
    writeFileSync(`${root}/images/existing.txt`, "original");
    await page.goto("/");
    const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
    const response = await page.request.post("/api/projects", { headers: { "x-acc-csrf": csrf },
      data: { slug: `uploads-${width}`, name: "Upload demo", workspace_path: root } });
    expect(response.status()).toBe(201);
    await page.goto(`/projects/${(await response.json()).id}?tab=files`);
    await page.getByRole("button", { name: fileRow("images", "Toggle") }).click();
    await expect(page.getByText("Upload to: images.", { exact: false })).toBeVisible();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Upload files", exact: true }).click();
    await (await chooser).setFiles([
      { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("button upload") },
      { name: "voice.wav", mimeType: "audio/wav", buffer: Buffer.from([82, 73, 70, 70, 0, 0, 0, 0, 87, 65, 86, 69]) },
    ]);
    const results = page.getByRole("list", { name: "Upload results" });
    await expect(results).toContainText("images/notes.txt: Uploaded");
    await expect(results).toContainText("images/voice.wav: Uploaded");
    expect(readFileSync(`${root}/images/notes.txt`, "utf8")).toBe("button upload");
    // A folder drop chooses that folder even when the current target is root.
    await page.getByRole("button", { name: "Use workspace root", exact: true }).click();
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.items.add(new File(["drop upload"], "dropped.txt", { type: "text/plain" }));
      data.items.add(new File(["replacement"], "existing.txt", { type: "text/plain" }));
      return data;
    });
    await page.getByRole("button", { name: fileRow("images", "Toggle") }).dispatchEvent("drop", { dataTransfer: transfer });
    await transfer.dispose();
    await expect(results).toContainText("images/dropped.txt: Uploaded");
    await expect(page.getByRole("button", { name: "Replace existing.txt", exact: true })).toBeVisible();
    expect(readFileSync(`${root}/images/existing.txt`, "utf8")).toBe("original");
    // A later edit makes the first explicit replacement stale; no content is lost.
    writeFileSync(`${root}/images/existing.txt`, "changed elsewhere");
    await page.getByRole("button", { name: "Replace existing.txt", exact: true }).click();
    await expect(results).toContainText("Already exists or changed");
    await expect(page.getByRole("button", { name: "Replace existing.txt", exact: true })).toBeEnabled();
    expect(readFileSync(`${root}/images/existing.txt`, "utf8")).toBe("changed elsewhere");
    await page.getByRole("button", { name: "Keep both", exact: true }).click();
    await expect(results).toContainText("images/existing (1).txt: Uploaded");
    expect(readFileSync(`${root}/images/existing (1).txt`, "utf8")).toBe("replacement");
    // Explicit cancel, explicit replace and per-file oversize refusal.
    const input = page.getByLabel("Choose files to upload");
    await input.setInputFiles({ name: "existing.txt", mimeType: "text/plain", buffer: Buffer.from("confirmed replace") });
    await expect(page.getByRole("button", { name: "Cancel upload", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Cancel upload", exact: true }).click();
    await expect(results).toContainText("Cancelled; existing file kept.");
    await input.setInputFiles({ name: "existing.txt", mimeType: "text/plain", buffer: Buffer.from("confirmed replace") });
    await page.getByRole("button", { name: "Replace existing.txt", exact: true }).click();
    await expect(results).toContainText("Replaced images/existing.txt (the previous copy is kept as");
    const kept = (await results.innerText()).match(/images\/\.rynhult-replaced-[0-9]+-[a-f0-9]+/)![0];
    expect(readFileSync(`${root}/${kept}`, "utf8")).toBe("changed elsewhere");
    expect(readFileSync(`${root}/images/existing.txt`, "utf8")).toBe("confirmed replace");
    await input.setInputFiles({ name: "large.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(4 * 1024 * 1024 + 1) });
    await expect(results).toContainText("File exceeds the 4 MiB per-file limit.");
    await expect(page.getByRole("button", { name: "Upload files", exact: true })).toBeEnabled();
    await expect(page.getByRole("button", { name: fileRow("images/dropped.txt") })).toBeVisible();
    await page.getByRole("button", { name: "Use workspace root", exact: true }).click();
    const rootDrop = await page.evaluateHandle(() => {
      const data = new DataTransfer(); data.items.add(new File(["root drop"], "root.txt")); return data;
    });
    await page.getByRole("region", { name: "Folder tree", exact: true }).dispatchEvent("drop", { dataTransfer: rootDrop });
    await rootDrop.dispose();
    await expect(results).toContainText("root.txt: Uploaded");
    expect(readFileSync(`${root}/root.txt`, "utf8")).toBe("root drop");
    await page.screenshot({ path: wikiShotPath(testInfo, width === 390 ? "files-upload-phone.png" : "files-upload.png"), fullPage: true });
    const protectedDrop = await page.evaluateHandle(() => {
      const data = new DataTransfer(); data.items.add(new File(["must not write"], "denied.txt")); return data;
    });
    await page.locator('[data-upload-blocked="true"]').first().dispatchEvent("drop", { dataTransfer: protectedDrop });
    await protectedDrop.dispose();
    await expect(page.getByRole("alert", { name: "File upload error", exact: true })).toContainText("That destination is protected.");
    await input.setInputFiles([
      { name: ".env", mimeType: "text/plain", buffer: Buffer.from("not allowed") },
      { name: "allowed.txt", mimeType: "text/plain", buffer: Buffer.from("allowed") },
    ]);
    await expect(results).toContainText("allowed.txt: Uploaded");
    await expect(results).toContainText(".env:");
    await expect(results).toContainText("protected");
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    const accessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(accessibility.violations).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`files-upload-${width}.png`), fullPage: true });
  });
}

test("Files upload completion preserves an editor draft made during transfer", async ({ page }) => {
  const root = `${info().workspace}/upload-draft`;
  mkdirSync(root, { recursive: true });
  writeFileSync(`${root}/notes.txt`, "original text");
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const response = await page.request.post("/api/projects", { headers: { "x-acc-csrf": csrf },
    data: { slug: "upload-draft", name: "Draft demo", workspace_path: root } });
  expect(response.status()).toBe(201);
  await page.goto(`/projects/${(await response.json()).id}?tab=files`);
  await page.getByRole("button", { name: fileRow("notes.txt") }).click();
  const editor = page.getByRole("textbox", { name: "File content" });
  await expect(editor).toContainText("original text");
  let releaseRead!: () => void;
  const heldRead = new Promise<void>((resolve) => { releaseRead = resolve; });
  let readStarted!: () => void;
  const reading = new Promise<void>((resolve) => { readStarted = resolve; });
  await page.route("**/api/files/project/*/read?*", async (route) => {
    const response = await route.fetch(); readStarted(); await heldRead; await route.fulfill({ response });
  });
  // A clean refresh starts before typing, but its changed disk version arrives later.
  writeFileSync(`${root}/notes.txt`, "external edit during refresh");
  await page.getByRole("button", { name: "Refresh files", exact: true }).click();
  await reading;
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let started!: () => void;
  const pending = new Promise<void>((resolve) => { started = resolve; });
  await page.route("**/api/files/project/*/upload", async (route) => { started(); await held; await route.continue(); });
  try {
    await page.getByLabel("Choose files to upload").setInputFiles({ name: "new.txt", mimeType: "text/plain", buffer: Buffer.from("new upload") });
    await pending;
    await expect(page.getByRole("progressbar", { name: "Batch upload progress" })).toBeVisible();
    await editor.fill("unsaved draft during upload");
    releaseRead(); release();
    await expect(page.getByRole("list", { name: "Upload results" })).toContainText("new.txt: Uploaded");
    await expect(editor).toContainText("unsaved draft during upload");
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
    expect(readFileSync(`${root}/notes.txt`, "utf8")).toBe("external edit during refresh");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("heading", { name: "File changed on disk" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current version on disk" })).toContainText("external edit during refresh");
    await expect(editor).toContainText("unsaved draft during upload");
  } finally { releaseRead(); release(); }
});


test("Files upload error stays scoped beside an editor conflict alert", async ({ page }) => {
  const root = `${info().workspace}/upload-alerts`;
  mkdirSync(`${root}/.ssh`, { recursive: true });
  writeFileSync(`${root}/notes.txt`, "original");
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const created = await page.request.post("/api/projects", { headers: { "x-acc-csrf": csrf },
    data: { slug: "upload-alerts", name: "Upload alerts demo", workspace_path: root } });
  expect(created.status()).toBe(201);
  await page.goto(`/projects/${(await created.json()).id}?tab=files`);
  await page.getByRole("button", { name: fileRow("notes.txt") }).click();
  const editor = page.getByRole("textbox", { name: "File content" });
  await expect(editor).toContainText("original");
  await editor.fill("keep my draft");
  writeFileSync(`${root}/notes.txt`, "changed on disk");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("heading", { name: "File changed on disk" })).toBeVisible();
  const drop = await page.evaluateHandle(() => {
    const data = new DataTransfer(); data.items.add(new File(["denied"], "denied.txt")); return data;
  });
  await page.locator('[data-upload-blocked="true"]').first().dispatchEvent("drop", { dataTransfer: drop });
  await drop.dispose();
  const error = page.getByRole("alert", { name: "File upload error", exact: true });
  await expect(error).toHaveCount(1);
  await expect(error).toContainText("That destination is protected.");
  await expect(page.getByRole("heading", { name: "File changed on disk" })).toBeVisible();
  await expect(editor).toContainText("keep my draft");
  const accessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(accessibility.violations).toEqual([]);
});
