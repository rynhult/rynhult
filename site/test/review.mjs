// Browser review of a built site: axe (WCAG 2.2 A/AA), horizontal overflow and overflow at 320/390/1440 px and screenshots at 1440/390 px,
// light and dark. Serve site/dist first, then: node site/test/review.mjs http://127.0.0.1:PORT [screenshot folder]
// Uses Playwright and axe from web/node_modules (npm ci in web/); PLAYWRIGHT_BROWSERS_PATH as for the app's e2e tests.
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const require = createRequire(join(ROOT, "web/package.json"));
const { chromium } = require("@playwright/test");
const { default: AxeBuilder } = require("@axe-core/playwright");

const [base, shots] = process.argv.slice(2);
if (!base) throw new Error("usage: node site/test/review.mjs BASE_URL [SCREENSHOT_DIR]");
if (shots) mkdirSync(shots, { recursive: true });

// The pages come from the build's sitemap, so a coming-soon build is reviewed as built; a full build samples the docs.
const sitemap = await (await fetch(`${base}/sitemap.xml`)).text();
const built = [...sitemap.matchAll(/<loc>https:\/\/rynhult\.dev([^<]*)<\/loc>/g)].map((m) => m[1]);
const PAGES = [...new Set(["/", ...built.filter((p) => !p.startsWith("/docs/") || ["/docs/", "/docs/User-Guide/Install/",
  "/docs/User-Guide/Sessions/", "/docs/Internal/Architecture/"].includes(p)), "/404.html"] )];
const VIEWPORTS = [["320", { width: 320, height: 844 }], ["1440", { width: 1440, height: 900 }], ["390", { width: 390, height: 844 }]];

const browser = await chromium.launch();
let failures = 0;
for (const scheme of ["light", "dark"]) {
  for (const [label, viewport] of VIEWPORTS) {
    const context = await browser.newContext({ viewport, colorScheme: scheme, reducedMotion: "reduce", deviceScaleFactor: 1 });
    const page = await context.newPage();
    const external = [];
    page.on("request", (r) => { if (!r.url().startsWith(base)) external.push(r.url()); });
    const csp = [];
    page.on("console", (m) => { if (/Content Security Policy/i.test(m.text())) csp.push(m.text()); });
    let landingChrome;
    for (const path of PAGES) {
      await page.goto(base + path, { waitUntil: "load" });
      await page.keyboard.press('Tab');
      if (!(await page.locator('.skip').evaluate(el => el === document.activeElement))) throw new Error(`${path}: skip link is not first`);
      await page.keyboard.press('Enter');
      if (!(await page.locator('main').evaluate(el => el === document.activeElement))) throw new Error(`${path}: skip link does not focus main`);
      await page.evaluate(() => window.scrollTo(0, 0));
      const chrome = await page.evaluate(() => ['.site-header', '.site-footer'].map(selector => {
        const el = document.querySelector(selector);
        const css = getComputedStyle(el);
        return {markup: el.outerHTML.replace(/>\s+</g, '><').replace(/\s+/g, ' ').trim(),
          font: css.font, color: css.color, padding: css.padding};
      }));
      if (path === '/') landingChrome = chrome;
      else if (JSON.stringify(chrome) !== JSON.stringify(landingChrome)) throw new Error(`${path}: chrome differs from landing`);
      for (const list of await page.locator('.prose ol.steps').all()) {
        if (await list.evaluate(el => getComputedStyle(el).listStyleType) !== 'decimal') throw new Error(`${path}: install steps lost numbering`);
      }
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      const problems = [
        ...(overflow > 0 ? [`page scrolls sideways by ${overflow}px`] : []),
        ...axe.violations.map((v) => `${v.id}: ${v.nodes.length}× ${v.nodes[0].target.join(" ")}`),
      ];
      failures += problems.length;
      console.log(`${problems.length ? "FAIL" : "ok  "} ${scheme} ${label} ${path}${problems.map((p) => `\n       ${p}`).join("")}`);
      if (shots && label !== "320" && ["/", "/docs/", "/install/", "/download/", "/security/", "/license/", "/docs/User-Guide/Install/"].includes(path)) {
        const name = (path === "/" ? "home" : path.replace(/^\/|\/$/g, "").replace(/\//g, "-")).toLowerCase();
        await page.screenshot({ path: join(shots, `${name}-${label}-${scheme}.png`), fullPage: path !== "/docs/User-Guide/Install/" });
      }
    }
    if (external.length) { failures++; console.log(`FAIL requests to other servers: ${[...new Set(external)].join(", ")}`); }
    if (csp.length) { failures++; console.log(`FAIL CSP: ${csp.join("; ")}`); }
    await context.close();
  }
}
await browser.close();
console.log(failures ? `${failures} problem(s)` : "no problems");
process.exit(failures ? 1 : 0);
