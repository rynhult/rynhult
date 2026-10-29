// Screenshot every page of the UI review harness (scripts/ux_harness.py) in dark and light, desktop and phone.
//   node web/scripts/ux-shots.mjs <state-dir> <out-dir>
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";

const [state, out] = process.argv.slice(2);
const h = JSON.parse(readFileSync(`${state}/harness.json`, "utf8"));
mkdirSync(out, { recursive: true });
const api = async (path) => (await fetch(h.origin + path, { headers: { cookie: `acc_session=${h.cookie}` } })).json();
const projects = await api("/api/projects");
const sessions = await api("/api/sessions");
const project = projects.find((p) => p.slug === "demo-app") ?? projects[0];
const session = sessions.find((s) => s.observed_state !== "archived") ?? sessions[0];
const pages = [["overview", "/"], ["inbox", "/inbox"], ["sessions", "/sessions"], ["projects", "/projects"], ["project", `/projects/${project?.id}`],
  ["session", `/sessions/${session?.id}`], ["publishing", "/publishing"], ["catalog", "/catalog"], ["host", "/host"],
  ["providers", "/providers"], ["accounts", "/accounts"], ["audit", "/audit"]];
const browser = await chromium.launch();
for (const [device, viewport] of [["desktop", { width: 1440, height: 900 }], ["phone", { width: 390, height: 844 }]]) {
  for (const theme of ["dark", "light"]) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: device === "phone" ? 2 : 1, colorScheme: theme });
    const url = new URL(h.origin);
    await ctx.addCookies([{ name: "acc_session", value: h.cookie, domain: url.hostname, path: "/" },
      { name: "acc_csrf", value: h.csrf, domain: url.hostname, path: "/" }]);
    await ctx.addInitScript((t) => { try { localStorage.setItem("acc-theme", t); } catch {} }, theme);
    const page = await ctx.newPage();
    for (const [name, path] of pages) {
      await page.goto(h.origin + path);
      await page.waitForLoadState("networkidle").catch(() => {});
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${out}/${device}-${theme}-${name}.png`, fullPage: device === "desktop" });
    }
    if (device === "desktop" && theme === "dark") {
      await page.goto(h.origin + "/");
      await page.getByRole("button", { name: "New session" }).click();
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${out}/${device}-${theme}-new-session.png` });
    }
    await ctx.close();
  }
}
await browser.close();
console.log(`screenshots in ${out}`);
