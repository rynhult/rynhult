import { expect, test } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { AUTH_FILE, gitProject, routeDemoSessions, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

test('Rynhult shell and Guide use the public name; refresh remaining artificial-data screens', async ({ page }, testInfo) => {
  test.setTimeout(60000);
  const project = await gitProject(page, 'example-brand-guide');
  const wiki = `${project.folder}/docs/wiki`;
  mkdirSync(`${wiki}/User-Guide`, { recursive: true });
  writeFileSync(`${wiki}/.order`, 'Home\nUser-Guide\n');
  writeFileSync(`${wiki}/Home.md`, '# Demo API wiki\n\nStart with [Getting started](/User-Guide/Getting-Started).\n\n| Area | Status |\n|---|---|\n| Sessions | Ready |\n');
  writeFileSync(`${wiki}/User-Guide.md`, '# User guide\n');
  writeFileSync(`${wiki}/User-Guide/.order`, 'Getting-Started\n');
  writeFileSync(`${wiki}/User-Guide/Getting-Started.md`, '# Getting started\n\n1. Open the demo API.\n2. Start a session to add a greeting.\n');
  execFileSync('git', ['-C', project.folder, 'add', 'docs/wiki']);
  execFileSync('git', ['-C', project.folder, '-c', 'user.name=Alex Example', '-c', 'user.email=alex@example.com', 'commit', '-qm', 'Add example user guide']);
  await page.route(`**/api/projects/${project.id}`, async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, name: 'Demo API', slug: 'demo-api', workspace_path: '/srv/acc/workspaces/demo-api' } });
  });
  await routeDemoSessions(page);
  for (const [url, name] of [
    ['/sessions', 'sessions-list.png'],
    ['/publishing', 'publishing.png'],
    ['/search?q=fake', 'search.png'],
    [`/projects/${project.id}/wiki`, 'project-wiki.png'],
    [`/projects/${project.id}/wiki/User-Guide/Getting-Started`, 'project-wiki-page.png'],
    [`/projects/${project.id}`, 'project-page.png'],
  ]) {
    await page.goto(url);
    await page.waitForLoadState('networkidle');
    await expect(page).toHaveTitle(/ · Rynhult$/);
    await page.screenshot({ animations: 'disabled', path: wikiShotPath(testInfo, name) });
  }
  await page.getByRole('tablist', { name: 'Project sections' }).getByRole('tab', { name: 'Ports', exact: true }).click();
  await page.getByText('Reserved for this project: its services listen here, and only these can be published', { exact: true }).scrollIntoViewIfNeeded();
  await page.locator('#project-ports').scrollIntoViewIfNeeded();
  await page.screenshot({ animations: 'disabled', path: wikiShotPath(testInfo, 'project-ports.png') });
  await page.goto('/settings#budgets');
  await page.getByRole('heading', { name: 'Budgets', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ animations: 'disabled', path: wikiShotPath(testInfo, 'settings-budgets.png') });
  await page.goto('/');
  await page.getByRole('button', { name: 'Jump to…' }).first().click();
  const palette = page.getByRole('dialog');
  await expect(palette).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: wikiShotPath(testInfo, 'command-palette.png') });
  await page.keyboard.press('Escape');
  await page.goto('/guide/Home');
  await expect(page.locator('article').getByRole('heading', { name: 'Rynhult', exact: true })).toBeVisible();
  await expect(page.locator('article')).toContainText('formerly ACC');
});

test('brand assets load and the mark follows the surface theme', async ({ page }) => {
  await page.goto('/');
  for (const dark of [true, false]) {
    await page.evaluate(value => document.documentElement.classList.toggle('dark', value), dark);
    const ink = dark ? 'light' : 'dark';
    const mark = page.locator(`img[src="/rynhult-mark-${ink}.svg"]`).first();
    await expect(mark).toBeVisible();
    await expect.poll(() => mark.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
    await expect(page.locator(`img[src="/rynhult-mark-${dark ? 'dark' : 'light'}.svg"]`).first()).toBeHidden();
  }
  for (const path of ['/favicon.svg', '/favicon.ico', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-maskable-512.png', '/icons/apple-touch-icon.png']) {
    const response = await page.request.get(path);
    expect(response.ok()).toBe(true);
    expect(response.headers()['content-type']).toMatch(/^image\//);
  }
  await page.context().clearCookies();
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Rynhult', exact: true })).toBeVisible();
  await page.evaluate(() => document.documentElement.classList.remove('dark'));
  await expect(page.locator('img[src="/rynhult-mark-dark.svg"]')).toBeVisible();
});
