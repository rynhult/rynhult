import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });
const warning = "Agent sessions on this machine can run Windows programs and read your Windows files as you.";

test("owners can review, dismiss and restore each WSL machine warning", async ({ page }, testInfo) => {
  let dismissed = false;
  let writes = 0;
  const bodies: unknown[] = [];
  await page.route("**/api/nodes", route => route.fulfill({ json: [
    { id:"build", name:"build-box", status:"active", connected:true, accounts:["alex-agent"],
      info:{os:"linux",wsl:"2",version:"0.14.0"}, system:{kind:"wsl",label:"Linux (WSL2)",wsl:"2"},
      wsl_isolation:{interop_enabled:true,windows_drive_readable:true,accounts:{"alex-agent":true},warning:true,dismissed,revision:String(writes).repeat(64)} },
    { id:"dedicated", name:"dedicated-box", status:"active", connected:true, accounts:["blair-agent"],
      info:{os:"linux",wsl:"2"}, system:{kind:"wsl",label:"Linux (WSL2)",wsl:"2"},
      wsl_isolation:{interop_enabled:false,windows_drive_readable:false,accounts:{"blair-agent":false},warning:false,dismissed:false,revision:"a".repeat(64)} },
  ] }));
  await page.route("**/api/nodes/build-box/wsl-isolation", route => {
    expect(route.request().method()).toBe("PUT");
    expect(route.request().postDataJSON().expected_revision).toBe(String(writes).repeat(64));
    dismissed = route.request().postDataJSON().dismissed;
    bodies.push(route.request().postDataJSON());
    writes++;
    return route.fulfill({json:{}});
  });
  await page.goto("/host");
  const panel = page.getByRole("region", {name:"WSL isolation on build-box",exact:true});
  await expect(panel.getByText(warning,{exact:true})).toBeVisible();
  await expect(page.getByText(warning,{exact:true})).toHaveCount(1);
  await expect(panel.getByRole("link",{name:"WSL isolation guide"})).toHaveAttribute("href","/guide/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes");
  const machines = page.locator("section").filter({has:page.getByRole("heading",{name:"Machines",exact:true})});
  await machines.screenshot({path:wikiShotPath(testInfo,"wsl-isolation-warning.png")});
  expect((await new AxeBuilder({page}).include("section").withTags(["wcag2a","wcag2aa","wcag21aa"]).analyze()).violations).toEqual([]);
  await panel.getByRole("button",{name:"Dismiss WSL warning for build-box"}).click();
  const confirm = page.getByRole("dialog",{name:"Accept Windows access on build-box?"});
  await expect(confirm).toContainText("does not block Windows access");
  expect(writes).toBe(0);
  await confirm.screenshot({path:wikiShotPath(testInfo,"wsl-isolation-accept.png")});
  await confirm.getByRole("button",{name:"Accept risk",exact:true}).click();
  await expect(panel.getByText(warning,{exact:true})).toHaveCount(0);
  await expect(panel.getByText("Windows access warning dismissed by an owner.",{exact:true})).toBeVisible();
  expect(writes).toBe(1);
  // The dismissal names exactly the exposure that was on screen (SR6-7).
  expect(bodies[0]).toEqual({dismissed:true, expected_revision:"0".repeat(64), accepted:{interop_enabled:true, accounts:{"alex-agent":true}}});
  await page.reload();
  await expect(panel.getByText(warning,{exact:true})).toHaveCount(0);
  await panel.getByRole("button",{name:"Show WSL warning for build-box"}).click();
  await expect(panel.getByText(warning,{exact:true})).toBeVisible();
  expect(writes).toBe(2);
  await page.setViewportSize({width:390,height:844});
  await panel.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({path:wikiShotPath(testInfo,"wsl-isolation-warning-phone.png")});
  await panel.getByRole("link",{name:"WSL isolation guide"}).click();
  await expect(page.getByRole("heading",{name:"Isolation for dedicated WSL nodes",exact:true})).toBeVisible();
});

test("unknown isolation is not shown as safe and a rejected dismissal leaves its warning visible", async ({page}) => {
  await page.route("**/api/nodes", route => route.fulfill({json:[
    {id:"legacy",name:"legacy-box",status:"active",connected:false,accounts:[],info:{os:"linux",wsl:"2"},
      wsl_isolation:{interop_enabled:null,windows_drive_readable:null,accounts:{},warning:false,dismissed:false,revision:"a".repeat(64)}},
    {id:"dev",name:"dev-box",status:"active",connected:true,accounts:["alex-agent"],info:{os:"linux",wsl:"2"},
      wsl_isolation:{interop_enabled:false,windows_drive_readable:true,accounts:{"alex-agent":true},warning:true,dismissed:false,revision:"a".repeat(64)}},
  ]}));
  await page.route("**/api/nodes/dev-box/wsl-isolation", route => route.fulfill({status:403,json:{detail:"Owner authorization changed; sign in again"}}));
  await page.goto("/host");
  await expect(page.getByText("WSL isolation is not fully reported.",{exact:false})).toBeVisible();
  const panel = page.getByRole("region",{name:"WSL isolation on dev-box"});
  await panel.getByRole("button",{name:"Dismiss WSL warning for dev-box"}).click();
  await page.getByRole("dialog").getByRole("button",{name:"Accept risk",exact:true}).click();
  await expect(panel.getByRole("alert")).toContainText("Owner authorization changed");
  await expect(panel.getByText(warning,{exact:true})).toBeVisible();
});

test("a dismissal that no longer matches the reported exposure shows the warning again", async ({page}) => {
  await page.route("**/api/nodes", route => route.fulfill({json:[
    {id:"lab",name:"lab-box",status:"active",connected:true,accounts:["alex-agent","blair-agent"],info:{os:"linux",wsl:"2"},
      wsl_isolation:{interop_enabled:true,windows_drive_readable:true,accounts:{"alex-agent":false,"blair-agent":true},
        warning:true,dismissed:false,dismissal_outdated:true,revision:"a".repeat(64)}},
  ]}));
  await page.goto("/host");
  const panel = page.getByRole("region",{name:"WSL isolation on lab-box"});
  await expect(panel.getByText(warning,{exact:true})).toBeVisible();
  await expect(panel.getByText("Windows access changed since an owner dismissed this warning, so it is shown again.",{exact:true})).toBeVisible();
  await expect(panel.getByRole("button",{name:"Dismiss WSL warning for lab-box"})).toBeVisible();
});

test("a stale keyboard confirmation cannot overwrite another owner's restored warning", async ({page}, testInfo) => {
  await page.setViewportSize({width:390,height:844});
  let revision = "a".repeat(64);
  let reads = 0;
  let dismissed = false;
  let accepted: unknown = null;
  await page.route("**/api/nodes", route => { reads++; return route.fulfill({json:[{
    id:"build",name:"build-box",status:"active",connected:true,accounts:["alex-agent"],
    info:{os:"linux",wsl:"2",hostname:'<img src=x onerror=alert(1)>'},
    system:{kind:"wsl",label:"Linux (WSL2)",wsl:"2"},
    wsl_isolation:{interop_enabled:true,windows_drive_readable:true,accounts:{"alex-agent":true},warning:true,dismissed,revision},
  }]}); });
  await page.route("**/api/nodes/build-box/wsl-isolation", route => {
    const body = route.request().postDataJSON();
    if (body.expected_revision !== revision) return route.fulfill({status:409,json:{detail:"Windows access warning choice changed. Review the refreshed machine and try again."}});
    dismissed = body.dismissed;
    accepted = body.accepted;
    revision = "c".repeat(64);
    return route.fulfill({json:{}});
  });
  await page.goto("/host");
  const panel = page.getByRole("region",{name:"WSL isolation on build-box"});
  const dismiss = panel.getByRole("button",{name:"Dismiss WSL warning for build-box"});
  for (let i=0; i<100 && !(await dismiss.evaluate(el => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await expect(dismiss).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog",{name:"Accept Windows access on build-box?"});
  await expect(dialog).toBeVisible();
  // Another owner accepted and restored while this confirmation was open (ABA).
  const previousReads = reads;
  revision = "b".repeat(64);
  await expect.poll(() => reads, {timeout:20000}).toBeGreaterThan(previousReads);
  const accept = dialog.getByRole("button",{name:"Accept risk",exact:true});
  for (let i=0; i<20 && !(await accept.evaluate(el => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await expect(accept).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("alert")).toContainText("choice changed");
  await expect(panel.getByText(warning,{exact:true})).toBeVisible();
  await panel.screenshot({path:wikiShotPath(testInfo,"wsl-isolation-conflict-phone.png")});
  expect(dismissed).toBe(false);
  expect(await page.locator('img[src="x"]').count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await dismiss.click();
  await page.getByRole("dialog").getByRole("button",{name:"Accept risk",exact:true}).click();
  await expect(panel.getByText("Windows access warning dismissed by an owner.",{exact:true})).toBeVisible();
  // The dismissal names the exposure the owner was shown, so the server can refuse it if that changed too.
  expect(accepted).toEqual({interop_enabled:true,accounts:{"alex-agent":true}});
});
