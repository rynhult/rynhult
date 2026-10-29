import {test, expect, type Page} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {AUTH_FILE, wikiShotPath} from './helpers';

async function expectPublishingContainment(page: Page) {
  const size = await page.evaluate(() => {
    const main = document.querySelector('main')!;
    return {root:document.documentElement.scrollWidth,width:window.innerWidth,scroll:main.scrollWidth,client:main.clientWidth,
      overflow:[...main.querySelectorAll('*')].filter(el => el.getBoundingClientRect().right > main.getBoundingClientRect().right)
        .map(el => ({tag:el.tagName,text:el.textContent?.slice(0,80),class:el.className})).slice(-10)};
  });
  expect(size.root,JSON.stringify(size)).toBeLessThanOrEqual(size.width);
  expect(size.scroll,JSON.stringify(size)).toBeLessThanOrEqual(size.client);
}

test.use({storageState:AUTH_FILE});
for (const width of [320,390,1440]) for (const theme of ['light','dark'] as const) {
  test(`publishing current and audit at ${width}px ${theme}`, async ({page},testInfo) => {
    await page.setViewportSize({width,height:1000});
    await page.emulateMedia({colorScheme:theme});
    const base = {action:'publish',host:'demo.example.com',port:8100,access:'internal',
      requested_by:'alex',decided_by:'casey',created_at:'2026-10-29T10:00:00Z',
      project_id:1,port_owner:{project:'Demo API'},target_ip:'192.0.2.10'};
    await page.route('**/api/publish/addresses', r => r.fulfill({json:[]}));
    await page.route('**/api/publish/zones', r => r.fulfill({json:{configured:true,zones:[{id:'demo-zone',name:'example.com'}],public_ip:'192.0.2.10',internal_ip:'10.0.0.4'}}));
    await page.route(/\/api\/publish\?/, r => {
      const url = new URL(r.request().url());
      if (url.searchParams.get('view') === 'current') return r.fulfill({json:[
        {...base,id:'pub-live',status:'applied'},
        {...base,id:'pub-pending',host:`${'long-review-'.repeat(5)}.example.com`,target_ip:'2001:db8:abcd:ef01:2345:6789:abcd:ef01',status:'pending_approval',decided_by:null},
      ]});
      const host = url.searchParams.get('host') ?? '';
      const status = url.searchParams.get('status');
      const offset = Number(url.searchParams.get('offset') ?? 0);
      const rows = Array.from({length:27},(_,i) => ({...base,id:`old-${i}`,host:`old-${i}.example.com`,
        status:i === 0 ? 'removed' : 'failed',reason:`Unbroken-${'x'.repeat(180)}`,detail:i === 0 ? 'replaced by pub-live (port 8100)' : 'Caddy refused the route'}))
        .filter(row => row.host.includes(host) && (!status || (status === 'replaced' ? row.detail.startsWith('replaced by') : row.status === status)));
      return r.fulfill({json:{items:rows.slice(offset,offset+25),total:rows.length}});
    });
    await page.goto('/publishing?tab=current');
    await expect(page.getByRole('heading',{name:'Requests and published sites'})).toBeVisible();
    await expect(page.getByRole('heading',{name:'Rules in effect'})).toBeVisible();
    const live = page.getByRole('article',{name:'demo.example.com pub-live'});
    await expect(live).toContainText('Project: Demo API');
    await expect(live).toContainText('approved by casey');
    await expect(live).toContainText('DNS target: 192.0.2.10');
    await expect(page.getByText('Caddy refused the route',{exact:true})).toHaveCount(0);
    const currentShot = width === 320 ? page.getByRole("navigation",{name:"Current pages"})
      : page.locator("section").filter({has:page.getByRole("heading",{name:"Requests and published sites",exact:true})});
    await currentShot.screenshot({path:(theme === "light" ? wikiShotPath(testInfo,`publishing-current-${width}-${theme}.png`) : testInfo.outputPath(`publishing-current-${width}-${theme}.png`))});
    await page.getByRole('tab',{name:'Current',exact:true}).focus();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab',{name:'Audit',exact:true})).toBeFocused();
    await expect(page).toHaveURL(/tab=audit/);
    await expect(page.getByRole('tabpanel')).toHaveAccessibleName('Audit');
    await expect(page.getByRole('article')).toHaveCount(25);
    await expect(page.getByRole('article').first()).toContainText('replaced');
    await page.screenshot({path:(theme === "light" && width !== 320 ? wikiShotPath(testInfo,`publishing-audit-${width}-${theme}.png`) : testInfo.outputPath(`publishing-audit-${width}-${theme}.png`)),fullPage:false});
    expect((await new AxeBuilder({page}).include('main').analyze()).violations).toEqual([]);
    await expectPublishingContainment(page);
    await page.getByRole('button',{name:'Next page',exact:true}).click();
    await expect(page.getByRole('article')).toHaveCount(2);
    await page.getByLabel('Filter by status').selectOption('failed');
    await expect(page).not.toHaveURL(/page=2/);
    await page.getByLabel('Filter by host').fill('old-26');
    await expect(page.getByRole('article')).toHaveCount(1);
    await expect(page.getByRole('article')).toContainText('Requested by alex');
    await page.reload();
    await expect(page.getByLabel('Filter by host')).toHaveValue('old-26');
    await expect(page.getByRole('article')).toHaveCount(1);
    await page.getByLabel('Filter by host').fill('missing');
    await expect(page.getByText('No historical entries match these filters.')).toBeVisible();
    await page.getByRole('tab',{name:'Current',exact:true}).click();
    await page.goBack();
    await expect(page.getByRole('tab',{name:'Audit',exact:true})).toHaveAttribute('aria-selected','true');
    await expect(page.getByLabel('Filter by host')).toHaveValue('missing');
    // Direct links always select the requested panel independently of saved filters.
    await page.goto('/publishing?tab=current');
    await expect(page.getByRole('tab',{name:'Current',exact:true})).toHaveAttribute('aria-selected','true');
    await expectPublishingContainment(page);
    expect((await new AxeBuilder({page}).include('main').analyze()).violations).toEqual([]);
  });
}

for (const width of [320,390,1440]) test(`current pages reach old live rules and replacement checks ignore the selected page (${width})`, async ({page}) => {
  await page.setViewportSize({width,height:1000});
  const base = {action:'publish',port:8004,access:'internal',requested_by:'alex',decided_by:'casey',
    created_at:'2026-10-29T10:00:00Z',project_id:1,port_owner:{project:'Demo API'},target_ip:'192.0.2.10'};
  const live = {...base,id:'live-old',host:'demo.example.com',status:'applied'};
  const rows = [...Array.from({length:27},(_,i) => ({...base,id:`pending-${i}`,host:`pending-${i}.example.com`,status:'pending_approval'})),live];
  await page.route('**/api/publish/zones', r => r.fulfill({json:{configured:true,zones:[{id:'demo',name:'example.com'}],internal_ip:'10.0.0.4'}}));
  await page.route('**/api/publish/addresses', r => r.fulfill({json:[]}));
  await page.route(/\/api\/publish\?/, r => {
    const url = new URL(r.request().url());
    if (url.searchParams.get('view') === 'audit') return r.fulfill({json:{items:[],total:0}});
    const host = url.searchParams.get('host');
    const offset = Number(url.searchParams.get('offset') ?? 0);
    const limit = Number(url.searchParams.get('limit') ?? 25);
    return r.fulfill({json:(host ? rows.filter(row => row.host === host) : rows).slice(offset,offset+limit)});
  });
  await page.goto('/publishing?tab=current');
  await expect(page.getByRole('article')).toHaveCount(25);
  await expect(page.getByRole('article',{name:'demo.example.com live-old'})).toHaveCount(0);
  await page.getByLabel('Subdomain').fill('demo');
  await page.getByText('Another service or port',{exact:true}).click();
  await page.getByLabel('Local port').fill('8100');
  await page.getByRole('button',{name:'Publish',exact:true}).click();
  await expect(page.getByRole('dialog')).toContainText('demo.example.com serves port 8004');
  await page.getByRole('dialog').getByRole('button',{name:'Cancel',exact:true}).click();
  await page.getByRole('button',{name:'Next current page',exact:true}).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/current_page=2/);
  await expect(page.getByRole('article')).toHaveCount(3);
  await expect(page.getByRole('article',{name:'demo.example.com live-old'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Next current page',exact:true})).toBeDisabled();
  await page.reload();
  await expect(page.getByRole('article',{name:'demo.example.com live-old'})).toBeVisible();
  await page.getByRole('tab',{name:'Audit',exact:true}).click();
  await expect(page.getByText('No historical entries match these filters.')).toBeVisible();
  await page.getByRole('tab',{name:'Current',exact:true}).click();
  await expect(page.getByRole('article')).toHaveCount(3);
});
