import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject } from './helpers';
test.use({storageState:AUTH_FILE});
for (const width of [1440,390]) test(`image need offers a real outcome at ${width}`, async ({page}) => {
  await page.setViewportSize({width,height:900});
  const {id,headers} = await gitProject(page,'orchfind');
  const created = await page.request.post('/api/sessions',{headers,data:{project_id:id,name:'Orchfind lead',provider:'claude',worktree:true,
    orchestrator:{agents:[{agent:'claude',profile:null}],machines:['local'],at_once:1,total:2,depth:1,hours:1}}});
  expect(created.ok(),await created.text()).toBeTruthy();
  const lead = (await created.json()).id;
  try {
    const base = `/api/teams/${lead}/worklist`;
    const made = await page.request.post(base+'/items',{headers,data:{kind:'need',title:'Type the poster text',steps:'The poster image is unreadable.'}});
    expect(made.ok(),await made.text()).toBeTruthy();
    const item = await made.json();
    expect((await page.request.patch(`${base}/items/${item.id}`,{headers,data:{revision:item.revision,state:'done'}})).status()).toBe(422);
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem',{name:item.title,exact:true});
    await expect(card.getByRole('link',{name:'Check image access',exact:true})).toHaveAttribute('href','/settings#image-access');
    await expect(card.getByRole('link',{name:'Upload a reference',exact:true})).toHaveAttribute('href',`/projects/${id}#project-brief`);
    await expect(card.getByRole('button',{name:'Mark done',exact:true})).toHaveCount(0);
    await card.getByRole('button',{name:'Skip this input',exact:true}).click();
    await expect(card).toContainText('Skip this input; do not change requirements or constraints without asking.');
    const answered = (await (await page.request.get(base)).json()).items.find((x:{id:number})=>x.id===item.id);
    expect(answered.state).toBe('answered');
    expect(answered.answered_by).not.toBe('@api');
    await expectAxeClean(page);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`,{headers:await csrfHeaders(page)}); }
});
