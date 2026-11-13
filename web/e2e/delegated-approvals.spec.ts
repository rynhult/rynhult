import { projectWorkspace } from "./helpers";
import { expect, test } from './fixtures';
import AxeBuilder from '@axe-core/playwright';
import { AUTH_FILE, info, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

test('batch exact pending tools and show the real person and decision reason', async ({ page }) => {
  await page.goto('/providers');
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const headers = { 'X-ACC-CSRF': csrf, Origin: info().origin };
  const project = await page.request.post('/api/projects', { headers, data: {
    slug: `approvals-${Date.now()}`, name: 'Demo approvals', workspace_path: projectWorkspace(), host: 'local',
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

test('Security shows exact consent, handles a stale revoke and keeps revoked history',async({page})=>{
  let revoked=false,attempts=0;
  const consent={id:'a'.repeat(32),revision:1,issuer:'Demo owner',action:'port.reserve',team_id:'demo-lead',project_id:1,
    state:'active',effective_state:'eligible',reason:'exact_saved_consent',constraints:{range:'dev',max_reservations:2},expires_at:null};
  await page.route('**/api/approvals/consents?*',async route=>{
    const history=new URL(route.request().url()).searchParams.get('state')==='revoked';
    await route.fulfill({json:{consents:history?(revoked?[{...consent,state:'revoked',effective_state:'blocked'}]:[]):revoked?[]:[consent],next_cursor:null}});
  });
  await page.route(`**/api/approvals/consents/${consent.id}/revoke`,async route=>{
    expect(route.request().postDataJSON()).toEqual({revision:1});attempts++;
    if(attempts===1){await route.fulfill({status:409,json:{detail:'Consent changed; refresh before revoking'}});return;}
    revoked=true;await route.fulfill({json:{...consent,state:'revoked'}});
  });
  await page.goto('/settings#security');
  await expect(page.getByRole('heading',{name:'Saved delegated permissions'})).toBeVisible();
  await expect(page.getByText('max_reservations:',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Revoke permission',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Revoke permission',exact:true}).click();
  await expect(page.getByText('Consent changed; refresh before revoking',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Revoke permission',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Revoke permission',exact:true}).click();
  await expect(page.getByText('No active saved permissions in this scope.')).toBeVisible();
  await page.getByRole('button',{name:'Show revoked history'}).click();
  await expect(page.getByText('revoked · blocked · revision 1',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'Revoke permission',exact:true})).toHaveCount(0);
  await expectAxeClean(page);
});

test('Inbox renders delegated outcomes as escaped activity without an approval card',async({page})=>{
  await page.route('**/api/approvals/activity',route=>route.fulfill({json:{operations:[{
    id:'operation-1',action:'port.reserve',state:'completed',decision:{issuer:'<script>owner</script>',lead_id:'demo-lead'},
    result:{port:8001,range:'dev'},created_at:'2026-11-07T12:00:00Z'}],limit:50}}));
  await page.goto('/inbox');
  const digest=page.getByRole('region',{name:'Delegated activity'});
  await digest.getByText('1 delegated operations').click();
  await expect(digest.getByText('Allowed by saved permission from <script>owner</script> · lead demo-lead')).toBeVisible();
  await expect(digest.getByText('8001',{exact:true})).toBeVisible();
  await expect(digest.getByRole('button',{name:/approve|allow|decline/i})).toHaveCount(0);
  await expect(digest.locator('script')).toHaveCount(0);
});

test('Security shows bounded refused-attempt history without consent or enable controls',async({page})=>{
  await page.route('**/api/approvals/consents?*',route=>route.fulfill({json:{consents:[],next_cursor:null}}));
  await page.route('**/api/approvals/activity',route=>route.fulfill({json:{operations:[],refusals:[{
    id:1,at:'2026-11-07T12:00:00Z',lead_id:'demo-lead',action:'image.check',reason:'Allowance exhausted'}]}}));
  await page.goto('/settings#security');
  await expect(page.getByRole('heading',{name:'Refused delegated attempts'})).toBeVisible();
  await expect(page.getByText('image.check · Allowance exhausted · lead demo-lead',{exact:false})).toBeVisible();
  await expect(page.getByText('No active saved permissions in this scope.')).toBeVisible();
  await expect(page.getByRole('button',{name:'Enable delegation',exact:true})).toHaveCount(0);
});

test('Security reviews one atomic save and preserves draft after revision conflict',async({page})=>{
  let revision=0,saves=0;
  await page.route('**/api/approvals/configuration*',async route=>{
    if(route.request().method()==='GET'){
      await route.fulfill({json:{enabled:false,available:true,revision,security_revision:7,images:[],recipes:[]}});return;
    }
    expect(route.request().postDataJSON()).toEqual({enabled:true,revision,security_revision:7,consents:[]});
    saves++;
    if(saves===1){revision=1;await route.fulfill({status:409,json:{detail:'Delegation changed; refresh and review your draft.'}});return;}
    await route.fulfill({json:{revision:2,consents:[]}});
  });
  await page.goto('/settings#security');
  const setup=page.getByRole('region',{name:'Delegation enablement and consent'});
  const enabled=setup.getByRole('checkbox',{name:'Enable delegated approvals'});
  await expect(enabled).not.toBeChecked();
  await enabled.check();
  await setup.getByRole('button',{name:'Review delegation changes'}).click();
  await expect(setup.getByRole('status')).toContainText('delegation On');
  await setup.getByRole('button',{name:'Save delegation and permissions'}).click();
  await expect(setup.getByText('Delegation changed; refresh and review your draft.')).toBeVisible();
  await expect(enabled).toBeChecked();
  await setup.getByRole('button',{name:'Review delegation changes'}).click();
  await setup.getByRole('button',{name:'Save delegation and permissions'}).click();
  await expect.poll(()=>saves).toBe(2);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await expectAxeClean(page);
});

test('a refreshed unavailable team never drops a selected consent into an enable-only save',async({page})=>{
  let current=true,saves=0;
  await page.route('**/api/sessions',route=>route.fulfill({json:[{id:'delegated-demo',team_id:'delegated-demo',parent_session_id:null,owner:'alex',project_id:1,project:'Demo',name:'Demo team',provider:'claude',desired_state:'running',observed_state:'running',run_as:'alex-agent',host:'local',container:false,changes:null}]}));
  await page.route('**/api/approvals/configuration*',async route=>{
    if(route.request().method()==='POST'){
      saves++;
      expect(route.request().postDataJSON().consents).toHaveLength(1);
      current=false;await route.fulfill({status:409,json:{detail:'Team changed; review the draft.'}});return;
    }
    const selected=new URL(route.request().url()).searchParams.has('team_id');
    await route.fulfill({json:{enabled:true,available:true,revision:current?1:2,security_revision:7,images:[],recipes:[],
      ...(current&&selected?{scope:{project_id:1,team_id:'delegated-demo',account:'alex-agent',machine:'local',machine_id:'local',issuer:'alex'}}:{})}});
  });
  await page.goto('/settings#security');
  const setup=page.getByRole('region',{name:'Delegation enablement and consent'});
  await setup.getByRole('combobox',{name:'Build team'}).selectOption('delegated-demo');
  await setup.getByRole('checkbox',{name:'Allow bounded port reservations'}).check();
  await setup.getByRole('button',{name:'Review delegation changes'}).click();
  await setup.getByRole('button',{name:'Save delegation and permissions'}).click();
  await expect(setup.getByText('Team changed; review the draft.')).toBeVisible();
  await expect(setup.getByText('This team has no eligible whole-account consent scope.',{exact:false})).toBeVisible();
  await expect(setup.getByRole('button',{name:'Review delegation changes'})).toBeDisabled();
  expect(saves).toBe(1);
});

test('install review card names the numeric account UID and runtime',async({page})=>{
  const digest='d'.repeat(64),runtime='a'.repeat(32);
  await page.route('**/api/sessions',route=>route.fulfill({json:[{id:'delegated-install-demo',team_id:'delegated-install-demo',parent_session_id:null,owner:'alex',project_id:1,project:'Demo',name:'Demo install team',provider:'claude',desired_state:'running',observed_state:'running',run_as:'alex-agent',host:'local',container:false,changes:null}]}));
  await page.route('**/api/approvals/configuration*',async route=>{
    const selected=new URL(route.request().url()).searchParams.has('team_id');
    await route.fulfill({json:{enabled:true,available:true,revision:1,security_revision:7,images:[],
      recipes:selected?[{digest,recipe:{id:'reviewed-sample',version:'1.2.3',registry:'https://registry.npmjs.org',bins:{sample:'cli.js'},manifest:[{path:'cli.js'}],runtime_id:runtime,limits:{max_installs:5}}}]:[],
      ...(selected?{install_identity:{uid:1001,runtime_id:runtime},scope:{project_id:1,team_id:'delegated-install-demo',account:'alex-agent',machine:'local',machine_id:'local',issuer:'alex'}}:{})}});
  });
  await page.goto('/settings#security');
  const setup=page.getByRole('region',{name:'Delegation enablement and consent'});
  await setup.getByRole('combobox',{name:'Build team'}).selectOption('delegated-install-demo');
  await setup.getByRole('combobox',{name:'Reviewed recipe'}).selectOption(digest);
  await expect(setup.getByText(`UID 1001 · runtime ${runtime}.`,{exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
});

for (const width of [1440, 390]) test(`child grant refusal and chained Inbox card at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const stamp = '2026-11-11T12:00:00Z';
  const chain = 'Parent chain: root-session → parent-session → child-session';
  await page.route('**/api/attention**', route => route.fulfill({ json: [{
    id: 98765, kind: 'tool_request', severity: 'warn', source: 'tools', verified: true,
    title: 'Child needs the owning person', detail: `limited by parent-session: parent grant changed. ${chain}`,
    session_id: null, session_name: 'Child session', project: 'Example project',
    created_at: stamp, updated_at: stamp, acked_at: null, resolved_at: null,
    tool: { id: 'child-tool', manager: 'npm', name: 'sample', version: '1.0',
      account: 'demo-agent', host: 'local', operation: 'install', status: 'pending_approval' },
  }] }));
  await page.goto('/inbox?filter=approvals&item=98765');
  const card = page.getByRole('region', { name: 'Tool request: sample', exact: true });
  await expect(card.getByText(/limited by parent-session: parent grant changed/)).toBeVisible();
  await expect(card.getByText(/Parent chain: root-session → parent-session → child-session/)).toBeVisible();
  const bounds = await card.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  await expectAxeClean(page);
});

for (const width of [1440, 390]) test(`publication approval keeps structured parent provenance at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const stamp = '2026-11-11T12:00:00Z';
  const lineage = { parent_chain: ['rootcap01', 'parentcap01', 'childcap01'], root_owner: 'alex' };
  const publication = { ...lineage, id: 'pub-child-caps', action: 'publish', host: 'child.example.com', port: 8100,
    access: 'public', project_id: null, session_id: 'childcap01', requested_by: 'session:childcap01',
    reason: 'Agent request text contains no parent chain.', status: 'pending_approval', zone_name: null,
    detail: null, decided_by: null, created_at: stamp, decided_at: null, completed_at: null,
    replaces: { id: 'old-pub', port: 8101, access: 'internal', project: 'Prior project', decided_by: 'alex' }, port_owner: { project: 'Example project', label: null } };
  await page.route('**/api/attention**', route => route.fulfill({ json: [{ ...lineage,
    id: 98766, kind: 'publish_request', severity: 'warn', source: 'publisher', verified: true,
    title: 'Child publication request', detail: 'This detail is replaced by the publication summary.',
    session_id: 'childcap01', session_name: 'Child session', project: 'Example project',
    created_at: stamp, updated_at: stamp, acked_at: null, resolved_at: null,
    publish_id: publication.id, publish: publication,
  }] }));
  await page.route(/\/api\/publish(?:\?.*)?$/, route => route.fulfill({ json: [publication] }));
  await page.goto('/inbox?filter=approvals&item=98766');
  const details = page.getByRole('region', { name: 'Inbox item details' });
  const chain = 'Parent chain: rootcap01 → parentcap01 → childcap01 · Owning person: alex';
  await expect(details.getByText(chain, { exact: true })).toBeVisible();
  const bounds = await details.boundingBox();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  await expectAxeClean(page);
  await details.getByRole('button', { name: 'Review and approve', exact: true }).click();
  await expect(page.getByRole('dialog').getByText(chain, { exact: true })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.goto('/publishing');
  await expect(page.getByText(chain, { exact: true })).toBeVisible();
  await expectAxeClean(page);
  await page.getByRole('button', { name: 'Review and approve', exact: true }).click();
  await expect(page.getByRole('dialog').getByText(chain, { exact: true })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
});
