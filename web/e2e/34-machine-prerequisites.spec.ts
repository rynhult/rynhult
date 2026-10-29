import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

test.use({ storageState: AUTH_FILE });

/** The Guide scrolled to `id` and nothing near above it is still loading: a late screenshot above the heading is a
 * layout shift the assertion would otherwise race under load (the roadmap's anchor-scroll flake). */
async function expectGuideAnchor(page: Page, id: string) {
  const heading = page.locator(`#${id}`);
  await expect(heading).toBeVisible();
  await expect.poll(() => heading.evaluate((target) => {
    const top = target.getBoundingClientRect().top;
    return [...document.querySelectorAll("article img")].filter((image) =>
      image.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING
      && top - image.getBoundingClientRect().bottom < 2 * window.innerHeight
      && !(image as HTMLImageElement).complete).length;
  }), { message: `screenshots above #${id} finished loading`, timeout: 15_000 }).toBe(0);
  await expect(heading).toBeInViewport();
}

test("Add machine prerequisites precede commands and stay accessible at phone width", async ({ page }, testInfo) => {
  await page.route("**/api/nodes", route => route.request().method() === "POST"
    ? route.fulfill({ json: {
      id: "demo-prereqs", name: "build-box", code: "000000-000000-000000-000000", expires_in: 600,
      command: "rynhult-node enroll --primary https://acc.example.lan --trust system --code 000000-000000-000000-000000 --name build-box",
      linux: "curl -fsSL https://acc.example.lan/api/nodes/install.sh | sudo sh", version: "0.13.0", trust: "system", verified: true,
      installers: { windows: "https://example.com/Rynhult-Setup.exe", mac: "https://example.com/Rynhult.pkg" },
    } }) : route.continue());
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.goto("/host");
    await page.getByRole("button", { name: "Add machine" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a machine" });
    await dialog.getByPlaceholder("gaming-pc").fill("build-box");
    await dialog.getByRole("button", { name: "Create enrollment code" }).click();
    for (const [tab, required, guide, screenshot] of [
      ["Linux", "Python 3.12", "/guide/User-Guide/Install#add-a-linux-machine", "add-machine"],
      ["Windows (WSL)", "20 GB", "/guide/User-Guide/Install-Windows#before-you-start", "add-machine-windows"],
      ["Mac", "macOS 13", "/guide/User-Guide/Install-macOS#before-you-start", "add-machine-mac"],
    ]) {
      await dialog.getByRole("tab", { name: tab, exact: true }).click();
      const prerequisites = dialog.getByRole("region", { name: "Before you start" });
      await expect(prerequisites).toContainText(required);
      await expect(prerequisites.getByRole("link", { name: "Installation guide" })).toHaveAttribute("href", guide);
      expect(await prerequisites.evaluate(el => !!(el.compareDocumentPosition(el.parentElement!.querySelector("code")!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
      expect(await dialog.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
      const axe = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
      expect(axe.violations).toEqual([]);
      // Test output only: this dialog's enroll command and installer links are mocked, and the Guide's images come from
      // the real pre-v1 fixture in 10-app (wikiShotPath here overwrote them with the post-v1 installer variant).
      await page.screenshot({ path: testInfo.outputPath(`${screenshot}${width === 1440 ? "" : "-phone"}.png`) });
    }
    await dialog.getByRole("link", { name: "Installation guide" }).click();
    await expect(page.locator("#before-you-start")).toHaveText("Before you start");
    await expectGuideAnchor(page, "before-you-start");
    await page.goto("/guide/User-Guide/Install#add-a-linux-machine");
    await expectGuideAnchor(page, "add-a-linux-machine");
    await page.getByRole("link", { name: "Windows troubleshooting", exact: true }).click();
    await expect(page).toHaveURL(/Install-Windows#troubleshooting$/);
    await expectGuideAnchor(page, "troubleshooting");
  }
});

for (const width of [1440, 390]) {
  test(`Guide anchors wait for the page tree before scrolling at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    let releaseTree!: () => void;
    const treeGate = new Promise<void>(resolve => { releaseTree = resolve; });
    let releaseImage!: () => void;
    const imageGate = new Promise<void>(resolve => { releaseImage = resolve; });
    await page.route('**/api/wiki/guide/acc', async route => {
      await treeGate;
      await route.continue();
    });
    await page.route(url => url.pathname === '/api/wiki/guide/acc/attachment'
      && url.searchParams.get('path') === '.attachments/add-machine.png', async route => {
      await imageGate;
      await route.continue();
    });
    try {
      const article = page.waitForResponse(response => response.url().includes('/api/wiki/guide/acc/page?'));
      await page.goto('/guide/User-Guide/Install#add-a-linux-machine');
      await article;
      // Let the article query render while the independent tree query is still pending.
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect(page.locator('article')).toHaveCount(0);
      releaseTree();
      const heading = page.locator('#add-a-linux-machine');
      await expect(heading).toBeInViewport();
      const image = page.getByRole('img', { name: 'Add machine: the Linux tab with the command to run', exact: true });
      await expect(image).toHaveJSProperty('naturalWidth', 0);
      releaseImage();
      await image.evaluate(async (element: HTMLImageElement) => { await element.decode(); });
      await expect(heading).toBeInViewport();
    } finally {
      releaseTree();
      releaseImage();
    }
  });
}
