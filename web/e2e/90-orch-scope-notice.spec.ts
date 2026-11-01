/** Real scope edit -> expired notice -> Inbox Stop, on disposable fake-provider state. */
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { environmentValue } from '../../src/acc/env.mjs';
import { AUTH_FILE, control, expectAxeClean, gitProject, info, teamRequest as team, waitForSessionState, waitForTeamStopped } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`missed scope offers Stop at ${width}px in ${theme}`, async ({page}, ti) => {
    test.setTimeout(90000);
    await page.setViewportSize({width, height:900}); await page.emulateMedia({colorScheme:theme});
    const {id, headers} = await gitProject(page, 'scope-notice');
    const launched = await page.request.post('/api/sessions', {headers, data:{project_id:id, provider:'claude', name:'Scope lead', worktree:true,
      orchestrator:{agents:[{agent:'claude', profile:null}], machines:['local'], at_once:1, total:2, depth:1, hours:1}}});
    expect(launched.ok(), await launched.text()).toBeTruthy();
    const lead = (await launched.json()).id;
    try {
      await waitForSessionState(page, lead, 'idle', 'claude_hook');
      const plan = await page.request.patch(`/api/teams/${lead}/plan`, {headers, data:{version:0, idea:'CSV import',
        scope:{in:['CSV import'],out:[],parked:[]}, requirements:[{text:'Import CSV locally'}],
        phases:[{key:'import',title:'Import CSV',requirement_ids:['R-1']}]}});
      expect(plan.ok(), await plan.text()).toBeTruthy();
      expect((await team(lead,'plan_ready',{reason:'Fixture ready'})).ok).toBeTruthy();
      const assigned = await team(lead,'assign',{goal:'Import CSV',task_type:'implementation',requirement_ids:['R-1'],acceptance:['Local CSV works'],
        need:{role:'general',classification_reason:'Small bounded import'}});
      expect(assigned.ok, JSON.stringify(assigned)).toBeTruthy();
      const selected = await team(lead,'select',{assignment:assigned.result.id});
      expect(selected.ok, JSON.stringify(selected)).toBeTruthy();
      const started = await team(lead,'start',{assignment:assigned.result.id,selection:selected.result.id,name:'CSV scope worker'});
      expect(started.ok, JSON.stringify(started)).toBeTruthy();
      const helper = started.result.id;
      await waitForSessionState(page, helper, 'idle', 'claude_hook');
      control(helper, 'permission');
      await waitForSessionState(page, helper, 'waiting_input', 'claude_hook');
      const current = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
      const changed = await page.request.patch(`/api/teams/${lead}/plan`, {headers, data:{version:current.version,
        requirements:[{id:'R-1',text:'Never upload customer CSV data'}]}});
      expect(changed.ok(), await changed.text()).toBeTruthy();
      const version = (await changed.json()).version;
      let deliveryId = '';
      await expect.poll(async () => {
        const response = await page.request.get(`/api/sessions/${helper}/deliveries`);
        expect(response.ok()).toBe(true);
        const { deliveries } = await response.json();
        const notice = deliveries.find((row: { preview: string }) => row.preview.includes(`Plan change notice version ${version}:`));
        deliveryId = notice?.id ?? '';
        return notice?.state;
      }, { message: 'Waiting for this scope version to be queued to the waiting helper', timeout: ti.timeout }).toBe('queued');
      execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c',
        "import sqlite3,sys\nwith sqlite3.connect(sys.argv[1]) as db:\n row=db.execute(\"UPDATE message_delivery SET expires_at = datetime('now', '-1 second') WHERE id=? AND session_id=? AND state='queued'\",(sys.argv[2],sys.argv[3]))\n assert row.rowcount == 1, 'Expected exactly this queued scope notice'",
        `${info().state}/data/acc.db`, deliveryId, helper]);
      await page.goto('/inbox');
      const notice = page.locator('div.grid').filter({has:page.locator(`a[href="/sessions/${helper}"]`)}).last();
      const stop = notice.getByRole('button',{name:'Stop helper',exact:true});
      await expect(stop).toBeVisible({ timeout: ti.timeout });
      await expect(notice.getByText(/CSV scope worker has not received scope version/)).toBeVisible();
      await notice.screenshot({path:ti.outputPath('scope-notice.png')});
      await expectAxeClean(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await stop.click();
      const modal = page.getByRole('dialog', {name:'Stop this helper?',exact:true});
      await expect(modal).toContainText('The accepted scope change stays saved');
      await modal.getByRole('button',{name:'Cancel',exact:true}).click(); await expect(stop).toBeFocused();
      await stop.click();
      await page.getByRole('dialog', {name:'Stop this helper?',exact:true}).getByRole('button',{name:'Stop helper',exact:true}).click();
      await expect.poll(async () => (await (await page.request.get(`/api/sessions/${helper}`)).json()).desired_state,
        { message: 'Waiting for the confirmed Stop helper action', timeout: ti.timeout }).toBe('stopped');
      const refused = await team(lead,'finish',{helper});
      expect(refused.ok).toBe(false); expect(refused.error).toContain('scope version');
    } finally {
      expect((await page.request.post(`/api/sessions/${lead}/team/stop`,{headers})).ok()).toBe(true);
      await waitForTeamStopped(page, lead);
    }
  });
}
