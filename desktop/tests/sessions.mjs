// Browser-only supplement; account separation is a provisioning qualification gate.
import { chromium, expect } from '../../web/node_modules/@playwright/test/index.mjs';
import AxeBuilder from '../../web/node_modules/@axe-core/playwright/dist/index.mjs';
import { environmentValue } from '../../src/acc/env.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const { origin, token } = JSON.parse(input);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
  await context.addCookies([{ name: 'rynhult_desktop', value: token, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const page = await context.newPage();
  await page.goto(origin);
  await page.waitForLoadState('networkidle');
  const shot = name => page.screenshot({ path: environmentValue('ACC_UPDATE_WIKI_SCREENSHOTS') === '1'
    ? `docs/wiki/.attachments/${name}.png` : `.acc/artifacts/${name}.png` });
  await expect(page.getByText('This computer · Standalone', { exact: true }).first()).toBeVisible();
  await page.getByRole('link', { name: 'Providers', exact: true }).first().click();
  const providerSections = page.getByRole('navigation', { name: 'Provider sections' });
  await expect(providerSections).toBeVisible();
  for (const name of ['Agent software', 'Tools', 'Model connections']) {
    await expect(providerSections.getByRole('link', { name, exact: true })).toHaveCount(0);
  }
  await page.getByRole('button', { name: 'Re-login', exact: true }).first().click();
  await page.getByRole('dialog').getByRole('button', { name: 'Start re-login', exact: true }).click();
  const login = page.getByRole('dialog');
  await expect(login.getByRole('button', { name: 'Copy login link', exact: true })).toBeVisible();
  await login.getByRole('textbox', { name: '2. Paste the authorization code shown after you approve' }).fill('good-code');
  await login.getByRole('button', { name: 'Finish login', exact: true }).click();
  await expect(login.getByText('Logged in. Running sessions keep their conversations.')).toBeVisible();
  await login.getByRole('button', { name: 'Close', exact: true }).last().click();
  await page.getByRole('combobox', { name: 'Agent CLI', exact: true }).selectOption('codex');
  await page.getByRole('textbox', { name: 'Exact CLI version', exact: true }).fill('0.163.0');
  await page.getByRole('button', { name: 'Install or update CLI', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Install CLI', exact: true }).click();
  await expect(page.getByText('Installed version 0.163.0 in the agent account.')).toBeVisible();
  await shot('desktop-linux-providers');
  await page.getByRole('link', { name: 'Projects', exact: true }).first().click();
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await page.getByRole('dialog').getByLabel('Name', { exact: true }).fill('Desktop demo');
  await page.getByRole('dialog').getByRole('button', { name: 'Create project', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('link', { name: 'Desktop demo', exact: true }).first().click();
  const projectSections = page.getByRole('tablist', { name: 'Project sections' });
  await expect(projectSections).toBeVisible();
  for (const name of ['Backup', 'Ports', 'Runtime']) {
    await expect(projectSections.getByRole('tab', { name, exact: true })).toHaveCount(0);
  }
  await page.getByRole('button', { name: 'Allow native sessions for this project' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Allow native sessions', exact: true }).click();
  await expect(page.getByText('Native sessions approved for this project.')).toBeVisible();
  await expect(page.getByText('Broker unreachable', { exact: true })).toHaveCount(0, { timeout: 15000 });
  await shot('desktop-linux-project');
  const accessibility = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(accessibility.violations).toEqual([]);
  for (const provider of ['Claude Code', 'Codex']) {
    await page.getByRole('button', { name: 'New session', exact: true }).last().click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Rename', exact: true }).click();  // the automatic name shows until Rename
    await dialog.getByLabel('Name', { exact: true }).fill(`Desktop ${provider}`);
    if (provider === 'Codex') await dialog.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('codex');
    await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
    await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
    await expect(page.locator('.xterm-rows')).toContainText(provider === 'Codex' ? 'fake-codex ready' : 'fake-claude ready', { timeout: 15000 });
    await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
    const id = new URL(page.url()).pathname.split('/').pop();
    // Reopen proves broker terminal persistence across viewer detach.
    await page.reload();
    await expect(page.locator('.xterm-rows')).toContainText('ready', { timeout: 15000 });
    await page.screenshot({ path: '.acc/artifacts/desktop-linux-terminal.png' });
    const csrf = (await context.cookies()).find(c => c.name === 'acc_csrf').value;
    const stop = await page.request.post(`${origin}/api/sessions/${id}/stop`, { headers: { 'x-acc-csrf': csrf, Origin: origin } });
    expect(stop.status()).toBe(200);
    await page.getByRole('link', { name: 'Projects', exact: true }).first().click();
    await page.getByRole('link', { name: 'Desktop demo', exact: true }).first().click();
  }
  const macPage = await context.newPage();
  await macPage.route('**/api/desktop/status', route => route.fulfill({ json: {
    provisioned: false, setup_supported: true, setup_kind: 'macos-installer', account: 'rynd-501',
    protection: 'Not isolated · Separate agent account', projects: [],
  } }));
  await macPage.goto(origin);
  await expect(macPage.getByText('macOS Installer asks for administrator authorization once;', { exact: false })).toBeVisible();
  await expect(macPage.getByText('packaged desktop/setup.py', { exact: false })).toHaveCount(0);
  await macPage.screenshot({ path: '.acc/artifacts/mac-setup-browser-simulation.png' });
  await macPage.close();
  console.log('PASS: native owner bootstrap → project → consent → Claude/Codex → xterm → reload → stop; axe clean');
} finally { await browser.close(); }
