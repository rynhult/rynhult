import { expect, type Page } from '@playwright/test';
import { test } from './shell-state';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, verifyAgain, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
const legacy = {
  dark: ['#10191f', '#15232b', '#1c2e38', '#263b46', '#f2f5f4', '#a4b4be', '#93a7b4', '#abb6ff', '#ffd78d', '#ffa0a0'],
  light: ['#f0f3f3', '#ffffff', '#e8eff3', '#dae5ed', '#10191f', '#485d68', '#526671', '#414fa8', '#794200', '#aa1e24'],
};
const names = ['bg', 'panel', 'panel-2', 'panel-3', 'text', 'muted', 'faint', 'accent', 'wait', 'bad'];
async function resolvedTokens(page: Page, names: string[]) {
  return page.evaluate(names => {
    const probe = document.createElement('span'); document.body.append(probe);
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d')!;
    try { return Object.fromEntries(names.map(name => {
      probe.style.color = `var(--${name})`;
      ctx.fillStyle = getComputedStyle(probe).color; ctx.fillRect(0, 0, 1, 1);
      const rgb = [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
      return [name, '#' + rgb.map(n => n.toString(16).padStart(2, '0')).join('')];
    })); } finally { probe.remove(); }
  }, names);
}
async function tokens(page: Page) { const values = await resolvedTokens(page, names); return names.map(n => values[n]); }
async function preference(page: Page, enabled: boolean) {
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled } })).ok()).toBeTruthy();
}
function rgb(hex: string) { return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255); }
function contrast(a: number[], b: number[]) {
  const luminance = (c: number[]) => c.map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4).reduce((n, x, i) => n + x * [.2126, .7152, .0722][i], 0);
  const [lo, hi] = [luminance(a), luminance(b)].sort((x, y) => x - y); return (hi + .05) / (lo + .05);
}
async function checkContrast(page: Page, theme: 'dark' | 'light') {
  const palette = await resolvedTokens(page, ['bg', 'panel', 'panel-2', 'panel-3', 'text', 'muted', 'faint', 'accent', 'accent-strong', 'accent-ink', 'border-input', 'border-strong', 'ok', 'wait', 'bad', 'info', 'idle', 'accent-tint']);
  for (const surface of ['bg', 'panel', 'panel-2', 'panel-3']) {
    const bg = rgb(palette[surface]);
    for (const text of ['text', 'muted', 'faint', 'accent', 'ok', 'wait', 'bad', 'info', 'idle']) {
      expect(contrast(rgb(palette[text]), bg), `${text} on ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
    for (const edge of ['accent', 'border-input', 'border-strong']) expect(contrast(rgb(palette[edge]), bg), `${edge} on ${surface}`).toBeGreaterThanOrEqual(3);
    if (theme === 'light') {
      const previousSurface = legacy.light[names.indexOf(surface)];
      expect.soft(contrast(rgb(palette.accent), bg), `light accent/focus on ${surface} retains legacy contrast`)
        .toBeGreaterThanOrEqual(contrast(rgb(legacy.light[7]), rgb(previousSurface)));
    }
    for (const status of ['ok', 'wait', 'bad', 'info']) {
      const fg = rgb(palette[status]); const tint = fg.map((v, i) => .12 * v + .88 * bg[i]);
      expect(contrast(fg, tint), `${status} on its 12% badge tint over ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
  }
  for (const text of ['accent', 'muted', 'text', 'ok', 'wait', 'bad', 'info', 'idle']) {
    expect(contrast(rgb(palette[text]), rgb(palette['accent-tint'])), `${text} on selected tint`).toBeGreaterThanOrEqual(4.5);
  }
  for (const fill of ['accent', 'accent-strong']) expect(contrast(rgb(palette['accent-ink']), rgb(palette[fill]))).toBeGreaterThanOrEqual(4.5);
}

const disabledOpacity = (page: Page) => page.locator('body').evaluate(body => {
  const button = document.createElement('button'); button.disabled = true; button.className = 'disabled:opacity-50';
  body.append(button); const opacity = getComputedStyle(button).opacity; button.remove(); return opacity;
});

test('1440 mockup palette: project and session AA, safety states, and legacy colours on unmount', async ({ page }) => {
  test.slow();
  const state = await (await page.request.get('/api/admin-features')).json();
  let enabled = await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: state.revision } });
  if (!enabled.ok()) {
    await verifyAgain(page); const fresh = await (await page.request.get('/api/admin-features')).json();
    enabled = await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: fresh.revision } });
  }
  expect(enabled.ok()).toBeTruthy();
  const project = await gitProject(page, 'cedar-notes');
  const made = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Restore workshop notes' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json();
  await waitForSessionState(page, session.id, 'idle');
  // Visual-only policy fixture: no real protection is disabled or launch policy changed.
  await page.route('**/api/security/effective*', async route => {
    const response = await route.fetch(); const policy = await response.json();
    await route.fulfill({ response, json: { ...policy, mode: 'unrestricted', switches: Object.fromEntries(Object.keys(policy.switches).map(k => [k, false])) } });
  });
  const capture = process.env.ACC_UPDATE_SHELL_PALETTE_SCREENSHOTS === '1';
  if (capture) mkdirSync('../docs/handoffs/shell-palette', { recursive: true });
  try {
    for (const theme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: theme });
      await preference(page, false); await page.goto(`/projects/${project.id}`);
      await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toBeVisible();
      expect(await tokens(page)).toEqual(legacy[theme]);
      const before = await page.locator('body').evaluate(el => ({ background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color }));
      expect(await disabledOpacity(page)).toBe('0.5');
      await preference(page, true); await page.goto(`/projects/${project.id}`);
      await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
      expect(await disabledOpacity(page)).toBe('0.6');
      expect((await tokens(page))[0]).toBe(theme === 'dark' ? '#141719' : '#f6f5f2');
      await expect(page.locator('#shell-header').getByRole('link', { name: /^Security:.*Unrestricted/ })).toBeVisible();
      await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
      await expect(page).toHaveURL(/\/projects$/);
      const projectList = page.getByRole('complementary', { name: 'Context sidebar' });
      await projectList.getByRole('textbox', { name: 'Search projects', exact: true }).fill(project.slug);
      await projectList.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${project.slug}`) }).click();
      await expect(page.getByRole('main').getByRole('heading', { name: project.slug, exact: true, level: 1 })).toBeVisible();
      await expect(page.getByRole('region', { name: `${project.slug} context`, exact: true })).toBeVisible();
      await expect.soft(page.locator('.shell-main')).toHaveCSS('background-color', theme === 'dark' ? 'rgb(28, 32, 35)' : 'rgb(255, 254, 250)');
      await expect(page.locator('.shell-sidebar')).toHaveCSS('background-color', theme === 'dark' ? 'rgb(25, 28, 31)' : 'rgb(238, 237, 232)');
      // Overview cards are flat on the content surface; the header rule preserves the grouping.
      const rule = page.locator('.shell-project-page > header');
      await expect(rule).toHaveCSS('border-bottom-style', 'solid');
      await expect(rule).toHaveCSS('border-bottom-width', '1px');
      await checkContrast(page, theme); await expectAxeClean(page);
      const railButton = page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true });
      await railButton.focus();
      expect(await railButton.evaluate(el => parseFloat(getComputedStyle(el).outlineWidth))).toBeGreaterThanOrEqual(2);
      const accent = (await resolvedTokens(page, ['accent'])).accent;
      const outline = `rgb(${rgb(accent).map(n => Math.round(n * 255)).join(', ')})`;
      await expect(railButton).toHaveCSS('outline-color', outline);
      if (capture) {
        await page.getByRole('main').evaluate(node => { node.scrollTop = 0; });
        await page.screenshot({ path: `../docs/handoffs/shell-palette/project-${theme}.png` });
      }
      await page.goto(`/sessions/${session.id}`);
      await expect(page.locator('.xterm')).toBeVisible();
      await checkContrast(page, theme); await expectAxeClean(page);
      if (capture) await page.screenshot({ path: `../docs/handoffs/shell-palette/session-${theme}.png` });
      // A real in-app trip outside the frame must remove root overrides too.
      await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Tools', exact: true }).click();
      await page.getByRole('complementary', { name: 'Context sidebar' }).locator('a[href="/board"]').click();
      await expect(page).toHaveURL(/\/board$/);
      await expect(page.getByRole('heading', { name: 'Status board', exact: true })).toBeVisible();
      // Warm trips keep the frame so working views survive; its chrome and palette leave.
      await expect(page.locator('.shell[data-board]')).toHaveCount(1);
      await expect(page.locator('#shell-header')).toBeHidden();
      expect(await tokens(page)).toEqual(legacy[theme]);
      await expectAxeClean(page);
      await page.goBack(); await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
      expect((await tokens(page))[0]).toBe(theme === 'dark' ? '#141719' : '#f6f5f2');
      await preference(page, false); await page.goto(`/projects/${project.id}`);
      expect(await tokens(page)).toEqual(legacy[theme]);
      expect(await page.locator('body').evaluate(el => ({ background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color }))).toEqual(before);
    }
  } finally {
    await page.request.post(`/api/sessions/${session.id}/stop`, { headers: await csrfHeaders(page) });
  }
});
