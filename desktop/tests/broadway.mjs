// Real keyboard input for the native window: Chromium runs GTK Broadway's own web
// client, which forwards key events to the Tauri/WebKitGTK window as a display would.
// Line protocol on stdin: "click <x> <y>", "press <key>", "type <text>", "shot <path>"; replies "ok".
import { createInterface } from 'node:readline';
import { chromium } from '../../web/node_modules/@playwright/test/index.mjs';

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1060 } });
  await page.goto(process.argv[2]);
  await page.waitForTimeout(1500);
  console.log('ok');
  for await (const line of createInterface({ input: process.stdin })) {
    const [command, ...rest] = line.split(' ');
    const argument = rest.join(' ');
    if (command === 'click') await page.mouse.click(...rest.map(Number));
    else if (command === 'press') await page.keyboard.press(argument);
    else if (command === 'type') await page.keyboard.type(argument, { delay: 30 });
    else if (command === 'shot') await page.screenshot({ path: argument });
    else break;
    await page.waitForTimeout(400);
    console.log('ok');
  }
} finally {
  await browser.close();
}
