import {defineConfig} from '@playwright/test';
import base from './playwright.config';
export default defineConfig({...base,workers:1,repeatEach:3,projects:[
  {name:'setup',repeatEach:1,testMatch:/00-setup\.spec\.ts/,use:base.projects![0].use},
  {name:'containment',dependencies:['setup'],testMatch:/50-ux-final-containment\.spec\.ts/,use:base.projects![3].use},
  {name:'publishing-views',dependencies:['setup'],testMatch:/92-publishing-tabs\.spec\.ts/,use:base.projects![3].use},
  ...[320,390,1440].map(width => ({name:`publishing-addresses-${width}`,dependencies:['setup'],
    testMatch:/61-publish-addresses\.spec\.ts/,use:{...base.projects![3].use,viewport:{width,height:900}}})),
]});
