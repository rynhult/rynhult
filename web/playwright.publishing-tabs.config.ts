import {defineConfig} from '@playwright/test';
import base from './playwright.config';
export default defineConfig({...base,workers:1,projects:[
  {name:'setup',testMatch:/00-setup\.spec\.ts/,use:base.projects![0].use},
  {name:'publishing-tabs',dependencies:['setup'],testMatch:/(26-ux-inbox|40-dns|61-publish-addresses|68-ux-remaining-admin|92-publishing-tabs)\.spec\.ts/,use:base.projects![3].use},
]});
