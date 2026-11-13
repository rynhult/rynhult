import { expect, test } from './fixtures';
import { AUTH_FILE, csrfHeaders, gitProject, openAdvanced } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
 for (const authVariable of ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]) {
  test(`Claude sign-in defaults to subscription and previews the delivered keys at ${width}px with ${authVariable}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const project = await gitProject(page, `claude-signin-${width}`);
    const headers = await csrfHeaders(page);
    const keys: string[] = [];
    try {
      for (const [variable, name] of [[authVariable, 'Example Claude billing'], ['OPENAI_API_KEY', 'Example other key']]) {
        const made = await page.request.post('/api/vault/keys', { headers, data: { variable, name, value: 'example-signin-placeholder' } });
        expect(made.ok(), await made.text()).toBe(true);
        const key = await made.json(); keys.push(key.id);
        const given = await page.request.post(`/api/vault/keys/${key.id}/give`, { headers, data: { project_id: project.id } });
        expect(given.ok()).toBe(true);
      }
      await page.goto('/');
      await page.getByRole('button', { name: 'New session', exact: true }).first().click();
      const dialog = page.getByRole('region', { name: 'New session', exact: true });
      await openAdvanced(dialog);
      await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(project.id));
      await openAdvanced(dialog);
      await dialog.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('claude');
      const signin = dialog.getByRole('combobox', { name: 'Claude sign-in', exact: true });
      await expect(signin).toHaveValue('subscription');
      const delivered = dialog.getByTestId('session-keys');
      await expect(delivered).toContainText('Example other key');
      await expect(delivered).not.toContainText('Example Claude billing');
      const preview = page.waitForResponse(r => new URL(r.url()).pathname === '/api/sessions/preview'
        && r.request().postDataJSON().claude_auth === 'api_key');
      await signin.selectOption('api_key');
      expect((await preview).ok()).toBe(true);
      await expect(signin.locator('option:checked')).toHaveText(authVariable === 'CLAUDE_CODE_OAUTH_TOKEN'
        ? 'Subscription token: Example Claude billing' : 'API key: Example Claude billing (API billing)');
      await expect(delivered).toContainText('Example Claude billing');
      await signin.selectOption('subscription');
      await expect(delivered).not.toContainText('Example Claude billing');
      await expect(delivered).toContainText('Example other key');
      await dialog.getByRole('button', { name: 'Back to workspace', exact: true }).click();
    } finally {
      for (const id of keys) await page.request.delete(`/api/vault/keys/${id}`, { headers });
    }
  });
}
}
