// axe-core (WCAG 2.2 A/AA) scan of every page of the UI review harness, dark and light.
//   node web/scripts/ux-a11y.mjs <state-dir>
import AxeBuilder from "@axe-core/playwright";
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const h = JSON.parse(readFileSync(`${process.argv[2]}/harness.json`, "utf8"));
const api = async (p) => (await fetch(h.origin + p, { headers: { cookie: `acc_session=${h.cookie}` } })).json();
const projects = await api("/api/projects");
const sessions = await api("/api/sessions");
const pages = ["/", "/inbox", "/sessions", "/projects", `/projects/${projects[0].id}`, `/sessions/${sessions[0].id}`,
  `/sessions/${sessions[0].id}?tab=changes`, "/grid", "/search?q=refunds", "/settings",
  "/publishing", "/catalog", "/host", "/providers", "/audit"];
const browser = await chromium.launch();
let total = 0;
for (const [theme, width] of [["dark", 1440], ["light", 1440], ["dark", 390]]) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } });
  const host = new URL(h.origin).hostname;
  await ctx.addCookies([{ name: "acc_session", value: h.cookie, domain: host, path: "/" }, { name: "acc_csrf", value: h.csrf, domain: host, path: "/" }]);
  await ctx.addInitScript((t) => { try { localStorage.setItem("acc-theme", t); } catch {} }, theme);
  const page = await ctx.newPage();
  for (const path of pages) {
    await page.goto(h.origin + path);
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(500);
    const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).exclude(".xterm").analyze();
    for (const v of result.violations) {
      total += v.nodes.length;
      console.log(`[${theme}${width < 600 ? "/phone" : ""}] ${path} ${v.impact} ${v.id} (${v.nodes.length}): ${v.help}`);
      for (const n of v.nodes.slice(0, 2)) console.log(`    ${n.target.join(" ")} ${n.failureSummary?.split("\n")[1]?.trim() ?? ""}`);
    }
  }
  await ctx.close();
}
await browser.close();
console.log(`violations: ${total}`);
