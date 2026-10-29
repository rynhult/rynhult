import { expect, test } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

test.use({ storageState: AUTH_FILE });

type WikiNode = { path: string | null; children: WikiNode[] };

test("Guide section links reach existing rendered sections", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/guide");
  const response = await page.request.get("/api/wiki/guide/acc");
  expect(response.ok()).toBe(true);
  const tree = await response.json() as { pages: WikiNode[] };
  const paths = (nodes: WikiNode[]): string[] => nodes.flatMap(node => [
    ...(node.path ? [node.path.replace(/\.md$/, "")] : []), ...paths(node.children),
  ]);
  const sections = new Map<string, Set<string>>();
  const links: { from: string; target: string; fragment: string }[] = [];
  for (const path of paths(tree.pages)) {
    await page.goto(`/guide/${path}`);
    const article = page.locator("article.wiki-prose");
    await expect(article).toBeVisible();
    const rendered = await article.evaluate(element => ({
      ids: Array.from(element.querySelectorAll("[id]"), node => node.id),
      links: Array.from(element.querySelectorAll<HTMLAnchorElement>("a[href]"), node => node.href),
    }));
    sections.set(`/guide/${path}`, new Set(rendered.ids));
    for (const link of rendered.links) {
      const url = new URL(link);
      if (url.origin === new URL(page.url()).origin && url.pathname.startsWith("/guide/") && url.hash) {
        links.push({ from: path, target: decodeURIComponent(url.pathname), fragment: decodeURIComponent(url.hash.slice(1)) });
      }
    }
  }
  expect(links.length).toBeGreaterThan(0);
  const missing = links.filter(link => !sections.get(link.target)?.has(link.fragment));
  expect(missing.map(link => `${link.from}: ${link.target}#${link.fragment}`)).toEqual([]);
});
