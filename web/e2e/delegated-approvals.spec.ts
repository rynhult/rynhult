import { expect, test } from './fixtures';
import AxeBuilder from '@axe-core/playwright';
import { AUTH_FILE, info } from './helpers';

test.use({ storageState: AUTH_FILE });

test('batch exact pending tools and show the real person and decision reason', async ({ page }) => {
  await page.goto('/providers');
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const headers = { 'X-ACC-CSRF': csrf, Origin: info().origin };
  const project = await page.request.post('/api/projects', { headers, data: {
    slug: `approvals-${Date.now()}`, name: 'Demo approvals', workspace_path: info().workspace, host: 'local',
  } });
  expect(project.status()).toBe(201);
  const pid = (await project.json()).id;
  expect((await page.request.post(`/api/projects/${pid}/trust`, { headers })).ok()).toBe(true);
  const session = await page.request.post('/api/sessions', { headers,
    data: { project_id: pid, provider: 'claude', name: 'Demo approvals' } });
  expect(session.status()).toBe(201);
  const sid = (await session.json()).id;
  try {
    const ids: string[] = [];
    for (const name of ['prettier', 'typescript']) {
      const r = await page.request.post('/api/tools/requests', { headers, data: {
        session_id: sid, manager: 'npm', name, reason: 'Build the demo app',
      } });
      expect(r.status()).toBe(201);
      ids.push((await r.json()).id);
    }
    const preview = await page.request.post('/api/approvals/batches/preview', { headers, data: { requests: ids } });
    expect(preview.status()).toBe(201);
    const batch = await preview.json();
    expect(batch.requests.map((r: { name: string }) => r.name)).toEqual(['prettier', 'typescript']);
    expect((await page.request.post(`/api/approvals/batches/${batch.id}/approve`, {
      headers, data: { digest: batch.digest },
    })).ok()).toBe(true);
    await page.goto('/providers');
    const tools = page.getByRole('region', { name: 'Tools', exact: true });
    await tools.locator('summary').click();
    await expect(tools).toContainText('Decided by alex');
    await expect(tools).toContainText('Reviewed exact requests together');
    await expect(tools).toContainText('applied');
    expect((await new AxeBuilder({ page }).include('[aria-label="Tools"]').analyze()).violations).toEqual([]);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  } finally {
    await page.request.post(`/api/sessions/${sid}/stop`, { headers });
  }
});
