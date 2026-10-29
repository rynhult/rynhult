// Phone pass (rc): every main flow at 390 px, touch, both themes. Nothing may stick out sideways, tap targets are at
// least 44 px (inline text links excepted, as WCAG allows; a checkbox counts with its label), fixed or sticky bars
// never cover an input, and with the keyboard open the send button stays reachable.
import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { AUTH_FILE, control, csrfHeaders, info, sessionWithChanges } from "./helpers";
import type { AttentionItem } from "../src/lib/types";

test.use({ storageState: AUTH_FILE, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

type Issue = { page: string; kind: string; what: string; detail: string };

/** Problems on the current screen, as plain data (the page's own DOM, nothing private). */
async function audit(page: Page, label: string): Promise<Issue[]> {
  const found = await page.evaluate(() => {
    const issues: { kind: string; what: string; detail: string }[] = [];
    const width = window.innerWidth;
    const name = (el: Element) => {
      const text = (el.getAttribute("aria-label") || (el as HTMLElement).innerText || el.getAttribute("title") || "")
        .trim().replace(/\s+/g, " ").slice(0, 50);
      if (text) return `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""} "${text}"`;
      // Unnamed: its classes and the nearest ancestor with a name, so the report points at the component.
      const near = el.parentElement?.closest("[aria-label],[id],h1,h2,h3,section,[role]");
      return `${el.tagName.toLowerCase()}.${String(el.getAttribute("class") ?? "").split(/\s+/).slice(0, 6).join(".")}`
        + (near ? ` in ${near.tagName.toLowerCase()}${near.id ? "#" + near.id : ""}[${near.getAttribute("aria-label") ?? near.getAttribute("role") ?? ""}]` : "");
    };
    const visible = (el: Element) => {
      const r = el.getBoundingClientRect(), s = getComputedStyle(el);
      const clipped = s.clip !== "auto" && s.clip !== "" || s.clipPath.startsWith("inset(50%");  // sr-only until focused
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !clipped
        && !el.closest("[hidden],[inert]");
    };
    const scrolls = (el: Element | null): boolean => {
      for (let e = el; e && e !== document.body; e = e.parentElement) {
        const s = getComputedStyle(e);
        if (/(auto|scroll)/.test(s.overflowX) && e.scrollWidth > e.clientWidth) return true;
      }
      return false;
    };
    const clipped = (el: Element | null): boolean => {  // cut off by a box that fits (truncate, overflow hidden)
      for (let e = el; e && e !== document.body; e = e.parentElement) {
        const s = getComputedStyle(e);
        if (/(hidden|clip)/.test(s.overflowX) && e.getBoundingClientRect().right <= width + 1) return true;
      }
      return false;
    };
    if (document.documentElement.scrollWidth > width) {
      issues.push({ kind: "page scrolls sideways", what: "document", detail: `${document.documentElement.scrollWidth}px > ${width}px` });
    }
    for (const el of document.querySelectorAll("main *, header *, nav *, [role=dialog] *")) {
      if (!visible(el) || el.children.length > 0 && !(el as HTMLElement).innerText?.trim()) continue;
      const r = el.getBoundingClientRect();
      if (r.right > width + 1 && !scrolls(el.parentElement) && !clipped(el.parentElement)) {
        issues.push({ kind: "sticks out", what: name(el), detail: `right edge ${Math.round(r.right)}px > ${width}px` });
      }
    }
    const targets = "button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=checkbox], [role=switch]";
    for (const el of document.querySelectorAll(targets)) {
      if (!visible(el)) continue;
      const s = getComputedStyle(el);
      if (el.tagName === "A" && s.display === "inline") continue;  // a link in running text
      // Diff lines stay at code-line height (the whole row is the target); 44 px lines would make diffs unreadable.
      if (/^Comment on line /.test(el.getAttribute("aria-label") ?? "")) continue;
      let r = el.getBoundingClientRect();
      const label = (el as HTMLInputElement).labels?.[0] ?? el.closest("label");
      if (label && /^(checkbox|radio)$/.test((el as HTMLInputElement).type ?? "")) r = label.getBoundingClientRect();
      if (Math.round(r.height) < 44 || Math.round(r.width) < 44) {
        issues.push({ kind: "small tap target", what: name(el), detail: `${Math.round(r.width)}×${Math.round(r.height)}px` });
      }
    }
    const bars = [...document.querySelectorAll("body *")].filter((el) => {
      const p = getComputedStyle(el).position;
      return (p === "fixed" || p === "sticky") && visible(el);
    });
    for (const input of document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, select")) {
      if (!visible(input)) continue;
      const r = input.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) continue;  // only what's on screen
      for (const bar of bars) {
        if (bar.contains(input) || input.contains(bar)) continue;
        const b = bar.getBoundingClientRect();
        const overlap = Math.min(r.bottom, b.bottom) - Math.max(r.top, b.top);
        if (overlap > 4 && Math.min(r.right, b.right) - Math.max(r.left, b.left) > 4) {
          issues.push({ kind: "bar covers input", what: name(input), detail: `under ${name(bar)} by ${Math.round(overlap)}px` });
        }
      }
    }
    return issues;
  });
  return found.map((issue) => ({ page: label, ...issue }));
}

const found: Issue[] = [];

/** With the keyboard open (the visible area ~470 px tall), `target` must be on screen, scrolled to if need be, and
 *  nothing may sit on top of it. */
async function keyboardOpen(page: Page, label: string, target: import("@playwright/test").Locator) {
  await page.setViewportSize({ width: 390, height: 470 });
  try {
    await expect(target).toBeVisible();
    await target.scrollIntoViewIfNeeded();
    const hit = await target.evaluate((b) => {
      const r = b.getBoundingClientRect();
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return { onScreen: r.top >= 0 && r.bottom <= window.innerHeight, covered: !(top && (b === top || b.contains(top))),
               by: top && !(b === top || b.contains(top)) ? `${top.tagName.toLowerCase()} ${(top as HTMLElement).innerText?.slice(0, 30) ?? ""}` : "" };
    });
    if (!hit.onScreen || hit.covered) {
      found.push({ page: `${label}, keyboard open`, kind: "button hidden by keyboard or bar", what: await target.innerText(),
                   detail: `${hit.onScreen ? "on screen" : "off screen"}${hit.covered ? `, covered by ${hit.by}` : ""}` });
    }
  } finally {
    await page.setViewportSize({ width: 390, height: 844 });
  }
}
async function check(page: Page, label: string) {
  await page.waitForTimeout(400);  // let layout settle after navigation or a tab change
  found.push(...await audit(page, label));
}

test.afterAll(async ({}, testInfo) => {
  const unique = [...new Map(found.map((i) => [`${i.page}|${i.kind}|${i.what}`, i])).values()];
  writeFileSync(testInfo.outputPath("phone-pass.json"), JSON.stringify(unique, null, 1));
  console.log(`phone pass: ${unique.length} issues\n` + unique.map((i) => `${i.page} · ${i.kind} · ${i.what} · ${i.detail}`).join("\n"));
});

for (const theme of ["light", "dark"] as const) {
  test(`main flows at 390 px in ${theme}`, async ({ page }) => {
    test.setTimeout(300_000);
    await page.emulateMedia({ colorScheme: theme });
    await page.goto("/");
    const headers = await csrfHeaders(page);
    const project = await page.request.post("/api/projects", { headers, data: {
      slug: `phone-pass-${randomUUID().slice(0, 8)}`, name: "Phone pass", workspace_path: `${info().workspace}/../mobile-demo` } });
    expect(project.ok()).toBeTruthy();
    const pid = (await project.json()).id;
    expect((await page.request.post(`/api/projects/${pid}/trust`, { headers })).ok()).toBeTruthy();
    const launched = await page.request.post("/api/sessions", { headers, data: {
      project_id: pid, name: `phone-${theme}`, provider: "claude", initial_prompt: "Phone pass" } });
    expect(launched.ok()).toBeTruthy();
    const sid = (await launched.json()).id;
    // Another worker's waiting session can precede this test's own Inbox item.
    await page.route("**/api/attention?scope=open", async route => {
      const response = await route.fetch();
      const items: AttentionItem[] = await response.json();
      const own = items.find(item => item.kind === "needs_input" && item.session_id === sid);
      await route.fulfill({ response, json: own ? [
        { ...own, id: -1, session_id: "unrelated-session", title: "Another session needs your input" }, ...items,
      ] : items });
    });
    try {
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
      await check(page, `${theme} Overview`);

      control(sid, "permission");  // the agent asks: Inbox has something to answer
      await page.goto("/inbox");
      const answer = page.getByRole("link", { name: "Answer in terminal", exact: true })
        .and(page.locator(`[href="/sessions/${sid}"]`));
      await expect(answer).toBeVisible({ timeout: 20_000 });
      await check(page, `${theme} Inbox`);
      await answer.click();
      await expect(page).toHaveURL(new RegExp(`/sessions/${sid}`));
      await check(page, `${theme} Session terminal`);
      for (const [tab, label] of [["files", "Files"], ["changes", "Changes"], ["spec", "Setup"]] as const) {
        await page.goto(`/sessions/${sid}?tab=${tab}`);
        await check(page, `${theme} Session ${label}`);
      }
      // Keyboard open: the visual viewport shrinks to ~470 px. Commenting on a change: the textarea is focused, its
      // Add comment button must stay reachable; then Send to agent.
      const changed = await sessionWithChanges(page, `phonekb${theme}`);
      await page.goto(`/sessions/${changed}?tab=changes`);
      await page.getByRole("button", { name: /^Comment on line / }).first().click();
      const comment = page.getByPlaceholder("What should the agent change here?");
      await comment.fill("Please also update the docs");
      await keyboardOpen(page, `${theme} Changes comment`, page.getByRole("button", { name: "Add comment" }));
      await page.getByRole("button", { name: "Add comment" }).click();
      await keyboardOpen(page, `${theme} Changes send`, page.getByRole("button", { name: "Send to agent" }));
      await check(page, `${theme} Session Changes with a comment`);
      await page.request.post(`/api/sessions/${changed}/stop`, { headers });

      await page.goto("/");
      await page.getByRole("button", { name: "New session" }).first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await check(page, `${theme} New session`);
      await page.getByRole("dialog").getByPlaceholder("What should the agent work on?").fill("Fix the failing test");
      await keyboardOpen(page, `${theme} New session`, page.getByRole("dialog").getByRole("button", { name: /^(Trust and )?[Ll]aunch$/ }));
      await page.keyboard.press("Escape");

      await page.goto(`/projects/${pid}`);
      await check(page, `${theme} Project`);
      const sections = page.getByRole("tablist", { name: "Project sections" }).getByRole("tab");
      await expect(sections.first()).toBeVisible();
      for (let i = 0; i < await sections.count(); i++) {
        await sections.nth(i).click();
        await check(page, `${theme} Project ${(await sections.nth(i).innerText()).trim()}`);
      }

      await page.goto("/settings");
      const options = await page.locator("#settings-section option").evaluateAll((o) => o.map((x) => (x as HTMLOptionElement).value));
      for (const value of options.filter((v) => v !== "all")) {
        await page.goto(`/settings#${value}`);
        await check(page, `${theme} Settings ${value}`);
      }

      await page.goto("/providers");
      await check(page, `${theme} Providers`);
      await page.goto("/host");
      await check(page, `${theme} Host`);
      await page.getByRole("button", { name: "Add machine" }).click();
      await check(page, `${theme} Add machine`);
      await page.keyboard.press("Escape");
      await page.goto("/host/backups/setup");
      await check(page, `${theme} Backups setup`);

      await page.goto("/settings#access-devices");
      const pair = page.getByRole("button", { name: "Pair device" }).first();
      if (await pair.count()) {
        await pair.click();
        await check(page, `${theme} Pair a phone`);
      }
    } finally {
      await page.request.post(`/api/sessions/${sid}/stop`, { headers });
    }
    expect(found.filter((issue) => issue.page.startsWith(theme)), "phone-width problems (see the log)").toEqual([]);
  });
}
