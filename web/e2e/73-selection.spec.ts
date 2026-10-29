/** Slice-owned component seam; no shared TeamPanel/Settings mounts required for this fixture. */
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { resolve } from 'node:path';
import { build } from 'vite';
import react from '@vitejs/plugin-react';
import { expect, test } from '@playwright/test';

let script: string;
test.beforeAll(async () => {
  const entry=resolve('../.acc/artifacts/selection-entry.tsx');
  writeFileSync(entry, `import React from 'react';
    import {createRoot} from 'react-dom/client'; import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
    import {SelectionReasons,SelectionPreferences} from '${resolve('src/components/SelectionReasons.tsx')}';
    createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>
    <main><h1>Orchestrator selection</h1><SelectionReasons teamId="lead"/><SelectionPreferences/></main></QueryClientProvider>);`);
  try {
    const built=await build({configFile:false,define:{'process.env.NODE_ENV':'"production"'},plugins:[react()],resolve:{alias:{react:resolve('node_modules/react'),
      'react-dom':resolve('node_modules/react-dom'),'@tanstack/react-query':resolve('node_modules/@tanstack/react-query')}},
      build:{lib:{entry,formats:['iife'],name:'SelectionFixture'},write:false,minify:true}});
    const output=Array.isArray(built)?built[0]:built;
    if(!('output' in output)) throw new Error('fixture bundle missing');
    script=output.output.find(chunk=>chunk.type==='chunk')!.code;
  } finally { unlinkSync(entry); }
});

for (const width of [1440,390]) test(`selection reasons and ordered allow-lists at ${width}`, async ({page}) => {
  page.on('pageerror',error=>console.log('FIXTURE PAGE ERROR',error.message));
  let chosen=0, reason='', saved: unknown;
  const choices = [0,1].map(i=>({agent:i?'claude':'codex',profile:'backend',model:i?'model-b':'model-a',machine:'local',
    permission:'implement',quota:i?{status:'unknown'}:{status:'fresh',used_percent:20,headroom_percent:80},score:70-i,reasons:['Backend profile for a backend task','Owner allow-list preference'],
    eligible:true,rejections:[],est_cost:{value:.25,unit:'usage_percent',source:'price-class heuristic'}}));
  choices.push({...choices[0],agent:'copilot',eligible:false,rejections:['fresh provider quota reaches owner threshold'],quota:{status:'fresh',used_percent:95,headroom_percent:5}});
  const policy={version:1,rules:[{profile:'backend',task_type:'implementation',choices:[{agent:'codex',models:['model-a']},
                                                                                  {agent:'claude',models:['*']}]}]};
  await page.route('http://selection.example.lan/**', async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/fixture.js') return route.fulfill({contentType:'text/javascript',body:script});
    if(path==='/fixture.css') {
      const root=resolve('../src/acc/server/static');
      const manifest=JSON.parse(readFileSync(resolve(root,'.vite/manifest.json'),'utf8'));
      const css=manifest['index.html'].css as string[];
      return route.fulfill({contentType:'text/css',body:css.map(file=>readFileSync(resolve(root,file),'utf8')).join('\n')});
    }
    if(path.startsWith('/assets/')) return route.fulfill({path:resolve('../src/acc/server/static',path.slice(1))});
    if(path==='/api/teams/lead/selections') return route.fulfill({json:{selections:[{id:'s1',need:{task_type:'implementation'},
      state:reason?'overridden':'proposed',chosen,candidates:choices,reason:'Owner allow-list within the frozen grant',
      override_reason:reason,budget:{providers:{codex:{remaining:20}}}}]}});
    if(path.endsWith('/override')) {
      const body=route.request().postDataJSON(); chosen=body.choice; reason=body.reason;
      return route.fulfill({json:{ok:true}});
    }
    if(path==='/api/settings/orchestrator/selection') {
      if(route.request().method()==='PUT') { saved=route.request().postDataJSON(); return route.fulfill({json:saved}); }
      return route.fulfill({json:policy});
    }
    return route.fulfill({contentType:'text/html',body:'<!doctype html><html lang="en" class="dark"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Selection fixture</title><link rel="stylesheet" href="/fixture.css"><body><div id="root"></div><script src="/fixture.js"></script></body></html>'});
  });
  await page.setViewportSize({width,height:1000});
  await page.goto('http://selection.example.lan/');
  await expect(page.getByRole('heading',{name:'Selection reasons',exact:true})).toBeVisible();
  await page.waitForFunction(()=>getComputedStyle(document.documentElement).getPropertyValue('--bg').trim().length>0);
  await expect(page.getByText('Fresh provider quota: 20.0% used · 80.0% headroom.',{exact:true})).toBeVisible();
  await page.getByRole('heading',{name:'Selection reasons',exact:true}).locator('..').screenshot({path:`../.acc/artifacts/orchestrator-quota-reasons-${width}.png`});
  await page.getByText('Candidates and filters',{exact:true}).click();
  await expect(page.getByText('Unavailable: fresh provider quota reaches owner threshold',{exact:false})).toBeVisible();
  await expect(page.getByRole('option',{name:/copilot/})).toHaveJSProperty('disabled',true);
  await page.getByRole('combobox', {name:'Candidate',exact:true}).selectOption('1');
  await page.getByLabel('Reason', {exact:true}).fill('Independent check <script>not HTML</script>');
  await page.getByRole('button',{name:'Override selection',exact:true}).click();
  await expect(page.getByText('Independent check <script>not HTML</script>',{exact:true})).toBeVisible();
  expect(reason).toContain('<script>');
  await expect(page.getByText('Fresh provider quota:',{exact:false})).toHaveCount(0); // overridden candidate has unknown quota
  await expect(page.getByText('Provider quota is stale or unknown. Existing selection rules apply.',{exact:true})).toBeVisible();
  await page.getByRole('checkbox',{name:'Use fresh provider quota in selection'}).check();
  await page.getByLabel('Skip at usage percent',{exact:true}).fill('90');
  await page.getByLabel('Quota freshness in minutes',{exact:true}).fill('10');
  await page.getByLabel('Models for provider 1 in rule 1').fill('model-a,model-b');
  await page.getByRole('button',{name:'Save allow-lists',exact:true}).click();
  await expect.poll(()=>saved).toMatchObject({quota:{enabled:true,max_used_percent:90,max_age_minutes:10},rules:[{profile:'backend',choices:[{agent:'codex',models:['model-a','model-b']},{agent:'claude',models:['*']}]}]});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  const {default:AxeBuilder}=await import('@axe-core/playwright');
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await page.getByRole('group',{name:'Provider quota',exact:true}).screenshot({path:`../.acc/artifacts/orchestrator-quota-settings-${width}.png`});
  await page.screenshot({path:`../.acc/artifacts/orchestrator-selection-${width}.png`,fullPage:true});
});
