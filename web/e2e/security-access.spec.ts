import {expect,test} from '@playwright/test';
import {AUTH_FILE,csrfHeaders,expectAxeClean,freshStep,gitProject,loadSecret,PASSWORD,totp} from './helpers';
test.use({storageState:AUTH_FILE});
test.setTimeout(90000);

for(const width of [1440,390]) test(`Owner reviews filesystem access without losing draft at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:900});
  const project=await gitProject(page,`access-${width}`);
  const details=(await (await page.request.get('/api/projects')).json()).find((p:{id:number})=>p.id===project.id);
  await page.goto(`/settings/security/folders?machine=${encodeURIComponent(details.host)}&account=${encodeURIComponent(details.account)}`);
  const section=page.getByRole('region',{name:'Filesystem access',exact:true});
  await section.getByRole('button',{name:'Review filesystem access',exact:true}).click();
  const form=page.getByRole('dialog',{name:'Review filesystem access',exact:true});
  await form.getByLabel('Existing repository folder').fill('/home/alex/src/demo-api');
  await expect(form.getByLabel('Agent access')).toHaveValue('read');
  await form.getByLabel('Access method').selectOption('shared_group');
  await expect(form).toContainText('Other groups are not reused');
  await form.getByRole('button',{name:'Cancel',exact:true}).click();
  await section.getByRole('button',{name:'Review filesystem access',exact:true}).click();
  await expect(form.getByLabel('Existing repository folder')).toHaveValue('/home/alex/src/demo-api');
  await expectAxeClean(page);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  // A real request reaches only the test harness's unavailable root adapter.
  await freshStep(page);
  const headers=await csrfHeaders(page);
  const verified=await page.request.post('/api/auth/passkeys/reauth',{headers,data:{password:PASSWORD,code:totp(loadSecret())}});
  expect(verified.ok(),await verified.text()).toBe(true);
  await form.getByRole('button',{name:'Preview access changes'}).click();
  await expect(form).toContainText('folder service is unavailable');
  await expect(form.getByLabel('Existing repository folder')).toHaveValue('/home/alex/src/demo-api');
  await expect(page.getByRole('button',{name:'Grant access',exact:true})).toHaveCount(0);
});

test('UI simulation: explicit grant and separate Undo confirmations submit once and show repair honestly',async({page})=>{
  const project=await gitProject(page,'access-confirm');
  const details=(await (await page.request.get('/api/projects')).json()).find((p:{id:number})=>p.id===project.id);
  const preview={path:'/home/alex/src/demo-api',identity:[7,101,3],method:'named_acl',write:false,count:1,ancestors:['/home/alex'],ancestor_changes:[{path:'/home/alex',shared:false,access_before:[],access_after:[[1,7,-1],[2,1,1002],[4,0,-1],[16,1,-1],[32,0,-1]]}],changes:[{path:'.',owner:1001,group_before:1001,group_after:1001,mode_before:'0o700',mode_add:'none',access_before:[],access_after:[[1,7,-1],[2,5,1002],[4,0,-1],[16,5,-1],[32,0,-1]],default_before:[],default_after:[[2,5,1002]]}]};
  let applied=0,state='applied';
  const review=(undo=false)=>({id:undo?'undo-case':'grant-case',digest:'d'.repeat(64),scope:{machine:details.host,account:details.account},preview,undo,active_sessions:[],active_gates:[],warning:undo?'Undo cannot recall copied data; changed permissions require repair. Repository files are kept.':`${details.account} can read .env files, keys and untracked files. Removing access cannot recall copied data.`});
  await page.route('**/api/security/access**',async route=>{
    const p=new URL(route.request().url()).pathname;
    if(route.request().method()==='GET')return route.fulfill({json:{operations:applied?[{id:'grant-case',state,undo:false,approved:true,preview,result:state==='repair'?{conflicts:1,remaining_access:'A changed inode needs administrator repair.'}:undefined}]:[]}});
    if(p.endsWith('/preview')){expect(route.request().postDataJSON()).toMatchObject({write:false,method:'named_acl',path:preview.path});return route.fulfill({json:review()});}
    if(p.endsWith('/undo-preview'))return route.fulfill({json:review(true)});
    if(p.endsWith('/apply')){applied++;state=p.includes('undo-case')?'repair':'applied';return route.fulfill({json:{state}});}
    throw new Error(`Unexpected access request: ${p}`);
  });
  await page.goto(`/settings/security/folders?machine=${details.host}&account=${details.account}`);
  const section=page.getByRole('region',{name:'Filesystem access',exact:true});
  await section.getByRole('button',{name:'Review filesystem access',exact:true}).click();
  const form=page.getByRole('dialog',{name:'Review filesystem access',exact:true});
  await form.getByLabel('Existing repository folder').fill(preview.path);
  await form.getByRole('button',{name:'Preview access changes'}).click();
  const reviewed=page.getByRole('dialog',{name:'Review access grant',exact:true});
  await expect(reviewed).toContainText('.env files');
  await reviewed.getByText('Ancestor permission changes',{exact:true}).click();
  await expect(reviewed).toContainText('user 1002: --x');
  await reviewed.getByText(/Exact permission changes/).click();
  await expect(reviewed).toContainText('user 1002: r-x');
  await reviewed.getByRole('button',{name:'Grant access',exact:true}).click();
  const confirmation=page.getByRole('dialog',{name:'Grant filesystem access?',exact:true});
  await confirmation.getByRole('button',{name:'Grant access',exact:true}).dblclick();
  await expect(section).toContainText('Applied — agent access checked');expect(applied).toBe(1);
  await section.getByRole('button',{name:'Review Undo',exact:true}).click();
  const undo=page.getByRole('dialog',{name:'Review access Undo',exact:true});
  await expect(undo).toContainText('Repository files');
  await undo.getByRole('button',{name:'Undo access grant',exact:true}).click();
  await page.getByRole('dialog',{name:'Undo this access grant?',exact:true}).getByRole('button',{name:'Undo access grant',exact:true}).click();
  await expect(section).toContainText('Needs repair');expect(applied).toBe(2);
});
