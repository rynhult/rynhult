import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, expectAxeClean, gitProject, info, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

async function team(session: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${session}/events.jsonl`, JSON.stringify({acc_session: session,
    hook: {hook_event_name: 'AccTeam', request: {id, action, ...args}}}) + '\n');
  const file = `${info().run_dir}/${session}/team/${id}.json`;
  await expect.poll(() => existsSync(file), {timeout: 20_000}).toBeTruthy();
  const reply = JSON.parse(readFileSync(file, 'utf8'));
  expect(reply.ok, JSON.stringify(reply)).toBeTruthy();
  return reply.result;
}

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`assignments attempts and classification at ${width}px in ${theme}`, async ({page}, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({width, height: 900});
    await page.emulateMedia({colorScheme: theme});
    const {id, headers} = await gitProject(page, 'assignment');
    const created = await page.request.post('/api/sessions', {headers, data: {project_id: id, provider: 'claude', name: 'Synthetic lead',
      worktree: true, orchestrator: {agents: [{agent:'claude', profile:null}], machines:['local'], at_once:2, total:4, depth:1, hours:2}}});
    expect(created.ok(), await created.text()).toBeTruthy();
    const lead = (await created.json()).id as string;
    await team(lead, 'plan_update', {reason:'Synthetic test plan', idea:'Check the synthetic layout.',
      scope:{in:['Layout'],out:[],parked:[]}, phases:[{key:'layout',title:'Check layout'}]});
    await team(lead, 'plan_ready', {reason:'Bounded test scope'});
    const assignment = await team(lead, 'assign', {goal:'Check the synthetic layout without changing unrelated files', task_type:'implementation',
      acceptance:['Related checks pass'],requirement_ids:[], need:{role:'general',classification_reason:'Small task with the granted general role'}});
    const selected = await team(lead, 'select', {assignment:assignment.id,pin:{agent:'claude'}});
    expect(selected.chosen).not.toBeNull();
    const helper = await team(lead, 'start', {assignment:assignment.id,selection:selected.id,name:'Synthetic helper'});
    const replay = await team(lead, 'start', {assignment:assignment.id,selection:selected.id,name:'Same attempt'});
    expect(replay.id).toBe(helper.id);
    await team(helper.id,'report',{status:'partial',summary:'First evidence saved',findings_md:'# Evidence\nSynthetic findings only.'});
    await team(helper.id,'report',{status:'done',summary:'Synthetic layout checked'});
    let running: {id: string} | undefined;
    if (width === 1440 && theme === 'light') {
      const second = await team(lead, 'assign', {goal:'Second synthetic check', task_type:'implementation', acceptance:[], requirement_ids:[],
        need:{role:'general',classification_reason:'Synthetic running attempt'}});
      const picked = await team(lead, 'select', {assignment:second.id,pin:{agent:'claude'}});
      running = await team(lead, 'start', {assignment:second.id,selection:picked.id,name:'Running helper'});
    }
    if (width === 1440 && theme === 'dark') {
      for (let i=0; i<25; i++) await team(lead,'assign',{goal:`Synthetic future task ${i}`,task_type:'implementation',
        acceptance:[],requirement_ids:[],need:{role:'general',classification_reason:'Synthetic pagination evidence'}});
    }
    try {
      await page.goto(`/sessions/${lead}?tab=team#settings`);
      if (width === 1440 && theme === 'dark') await page.getByRole('button',{name:'Next assignments',exact:true}).click();
      const assignments = page.getByRole('region',{name:'Assignments',exact:true});
      await expect(assignments.getByText('Check the synthetic layout without changing unrelated files')).toBeVisible();
      await expect(assignments.getByText(/Attempt 1: reported/)).toBeVisible();
      await expect(assignments.getByText('Kept report versions: 1, 2')).toBeVisible();
      const first = assignments.getByRole('article', {name: `Assignment ${assignment.id}`});
      await expect(first.getByText('Mark attempt failed…')).toHaveCount(0);  // a reported attempt is settled
      if (running) {
        // The person's inspection action: refused while that helper runs, with the reason shown in place.
        const row = assignments.getByRole('listitem').filter({hasText: running.id});
        await row.getByText('Mark attempt failed…').click();
        const form = row.getByRole('form', {name: `Mark attempt ${running.id} failed`});
        await form.getByLabel('What you checked').fill('Synthetic inspection');
        await form.getByRole('button', {name: 'Mark attempt failed'}).click();
        await expect(form.getByText(/stop it and wait/)).toBeVisible();
      }
      await expect(page.getByText('Lead classification: implementation · general.', {exact:false}).first()).toBeVisible();
      await expect(page.getByText(`Catalog revision: ${selected.catalog_revision}`, {exact:false}).first()).toBeVisible();
      await expect(page.getByText(`Profile digest: ${selected.profile_digest}`, {exact:false}).first()).toBeVisible();
      await assignments.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expectAxeClean(page);
      await page.screenshot({path:wikiShotPath(testInfo,`orchestrator-assignments-${width}-${theme}.png`)});
      await first.getByRole('link',{name:'Open helper'}).focus();
      await page.keyboard.press('Enter');
      await expect(page).toHaveURL(new RegExp(`/sessions/${helper.id}`));
    } finally {
      const stopped = await page.request.post(`/api/sessions/${lead}/team/stop`, {headers});
      expect(stopped.ok(), await stopped.text()).toBeTruthy();
    }
  });
}
