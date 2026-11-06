import { expect, test } from './subscription-guard-fixture';
import { csrfHeaders } from './helpers';

const reason = 'Claude sessions signed in with a Claude subscription only take your own typing. Add ANTHROPIC_API_KEY in Settings / Keys and give it to this project to automate them. This switches this project to Claude API billing.';
test('subscription session shows the refusal before configuring automation', async ({ page, subscriptionSession: sid }) => {
  await page.goto(`/sessions/${sid}?tab=spec#automation`);
  const automation = page.getByRole('region', { name: 'Session automation' });
  await expect(automation.getByRole('status')).toContainText(reason);
  await expect(automation.getByRole('link', { name: 'Claude guide', exact: true })).toHaveAttribute('href', '/guide/Providers/Claude');
  await expect(automation.getByRole('link', { name: 'Use an API key for this project', exact: true }))
    .toHaveAttribute('href', /\/settings\?claude_api=1&project_id=\d+#keys$/);
  await automation.getByRole('button', { name: 'Goal and auto-resume', exact: true }).click();
  const settings = page.getByRole('dialog', { name: 'Goal and auto-resume' });
  await expect(settings.getByLabel('Resume when the limit resets')).toBeDisabled();
  await expect(settings.getByLabel('Goal text')).toBeDisabled();
  const result = await page.request.post(`/api/sessions/${sid}/message`, {
    headers: await csrfHeaders(page), data: { text: 'Automated feedback' },
  });
  expect(result.status()).toBe(409);
  expect((await result.json()).detail.code).toBe('claude_subscription_automation');
});

test('API key dialog names the project and group shortcut explains the group flow', async ({ page, subscriptionSession: sid }) => {
  const session = await (await page.request.get(`/api/sessions/${sid}`)).json();
  const projects = await (await page.request.get('/api/projects')).json();
  const project = projects.find((p: { id: number }) => p.id === session.project_id);
  expect(project).toBeTruthy();
  await page.goto(`/settings?claude_api=1&project_id=${project.id}#keys`);
  const dialog = page.getByRole('dialog', { name: 'Add a key' });
  await expect(dialog).toContainText(`The key will be given to ${project.name} when added.`);
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  // Group projects are deliberately excluded from the personal project list.
  await page.route('**/api/projects', route => route.fulfill({ json: [] }));
  await page.goto(`/sessions/${sid}?tab=spec#automation`);
  const automation = page.getByRole('region', { name: 'Session automation' });
  await expect(automation).toContainText('For group projects, ask a group administrator');
  await expect(automation.getByRole('link', { name: 'Use an API key for this project', exact: true })).toHaveCount(0);
});
