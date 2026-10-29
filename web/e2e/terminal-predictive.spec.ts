import { expect, test } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, info, resetTerminalThrottle } from './helpers';

test.use({ storageState: AUTH_FILE, serviceWorkers: 'block' });
let id: string;
test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('terminal-'), 'Run in the focused terminal matrix.');
  await resetTerminalThrottle(page);
  const project = await gitProject(page, 'predictive-input');
  const created = await page.request.post('/api/sessions', { headers: project.headers,
    data: { project_id: project.id, provider: 'claude', name: 'Predictive input' } });
  expect(created.ok()).toBe(true);
  id = (await created.json()).id;
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await page.locator('.xterm-helper-textarea').focus();
});
test.afterEach(async ({ page }) => {
  if (id) await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) });
});

for (const reader of [true, false]) test(`predictive replacement and composition never resend the line (reader ${reader})`, async ({ page }) => {
  if (!reader) await page.getByRole('checkbox', { name: 'Screen reader output', exact: true }).uncheck();
  const textarea = page.locator('.xterm-helper-textarea');
  await textarea.focus();
  await page.keyboard.type('This is me testing on my macb');
  await textarea.evaluate((field: HTMLTextAreaElement) => {
    // macOS text services can follow a native edit with a Process key. The
    // historical textarea retains the already-sent line; normalize its spaces
    // exactly as a text-service replacement does.
    field.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Process', keyCode: 229, which: 229 }));
    field.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertReplacementText', data: 'ook' }));
    field.value = field.value.replaceAll('\u00a0', ' ') + 'ook';
    field.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: 'ook' }));
  });
  await page.waitForTimeout(50); // flush xterm's native-change timer
  await textarea.evaluate((field: HTMLTextAreaElement) => {
    field.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    field.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertCompositionText', data: ' in Safari' }));
    field.value += ' in Safari';
    field.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: ' in Safari' }));
    field.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertCompositionText', data: ' in Safari', isComposing: true }));
    field.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: ' in Safari' }));
    field.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: ' in Safari' }));
  });
  await page.keyboard.type(' again again');
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${id}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('This is me testing on my macbook in Safari again again\n');
});

test('native input is consumed and predictive features are disabled', async ({ page }) => {
  const field = page.locator('.xterm-helper-textarea');
  await page.keyboard.type('ordinary typing');
  await expect(field).toHaveValue('');
  for (const name of ['autocomplete', 'autocorrect', 'autocapitalize']) await expect(field).toHaveAttribute(name, 'off');
  await expect(field).toHaveAttribute('spellcheck', 'false');
  await expect(field).toHaveAttribute('writingsuggestions', 'false');
});

test('native Latin edit after a Process key does not replay the accumulated prefix', async ({ page }) => {
  await page.keyboard.type('Please review ');
  await page.locator('.xterm-helper-textarea').evaluate(field => {
    field.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Process', keyCode: 229 }));
    if (!document.execCommand('insertText', false, 'task')) throw new Error('Native edit unavailable');
  });
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${id}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('Please review task\n');
});

for (const cancelled of [true, false]) test(`composition commits once and clears its field (cancelled ${cancelled})`, async ({ page }) => {
  await page.keyboard.type('Review ');
  await page.locator('.xterm-helper-textarea').evaluate((field: HTMLTextAreaElement, cancelled) => {
    field.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    field.value += '日本';
    field.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: '日本' }));
    field.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: cancelled ? '' : '日本' }));
    field.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '日本' }));
    field.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromComposition', data: cancelled ? '' : '日本' }));
  }, cancelled);
  await expect(page.locator('.xterm-helper-textarea')).toHaveValue('');
  await page.keyboard.type('done done');
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${id}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe(`Review ${cancelled ? '' : '日本'}done done\n`);
});

test('remounting the Safari editor keeps exactly one native input owner', async ({ page }) => {
  await page.keyboard.type('before-');
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await expect(page.locator('.xterm-helper-textarea')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  const take = page.getByRole('button', { name: 'Take control', exact: true });
  await expect(page.getByText('Read-write', { exact: true }).or(take)).toBeVisible();
  if (await take.isVisible()) await take.click();
  await page.locator('.xterm-helper-textarea').focus();
  await page.keyboard.type('after');
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${id}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('before-after\n');
});
