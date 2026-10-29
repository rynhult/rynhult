import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({
  ...base,workers:1,
  projects:[
    {name:'setup',testMatch:/00-setup\.spec\.ts/,use:base.projects![0].use},
    {name:'image-access',dependencies:['setup'],testMatch:/89-image-access\.spec\.ts/,use:base.projects![3].use},
  ],
});
