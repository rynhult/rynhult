// Performance check (with scripts/perf_check.py --keep): sign in as the synthetic owner, open the main pages and record every
// /api/ request each page makes, with its time to first byte. Optionally throttle like a mid-range phone and report
// how long the Overview takes to become usable.
//   node web/scripts/perf-pages.mjs <origin> <totp secret> <password> <session id> [--phone]
import { createHmac } from "node:crypto";
import { chromium, devices } from "@playwright/test";

const [origin, secret, password, sessionId, ...flags] = process.argv.slice(2);
const phone = flags.includes("--phone");

function base32(text) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of text.replace(/=+$/, "")) bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function totp(at = Date.now() / 1000) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30)));
  const digest = createHmac("sha1", base32(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

const browser = await chromium.launch();
const context = await browser.newContext(phone ? { ...devices["Pixel 7"], baseURL: origin } : { baseURL: origin });
// A code works once per 30 s step: start on a fresh step so an earlier sign-in can't make this one a replay.
await new Promise((resolve) => setTimeout(resolve, (30 - (Date.now() / 1000) % 30 + 1) * 1000));
const login = await context.request.post("/api/auth/login", {
  headers: { origin }, data: { username: "alex", password, code: totp() } });
if (!login.ok()) throw new Error(`login ${login.status()}: ${await login.text()}`);

const pages = [["Overview", "/"], ["Sessions", "/sessions"], ["Session detail", `/sessions/${sessionId}`],
               ["Inbox", "/inbox"], ["Host", "/host"], ["Settings", "/settings"], ["Project", "/projects/1"]];
const results = [];
for (const [name, path] of phone ? pages.slice(0, 1) : pages) {
  const page = await context.newPage();
  let cdp;
  if (phone) {  // Lighthouse's mobile profile: 4x CPU slowdown, ~1.6 Mbps down, 150 ms round trip
    cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions",
      { offline: false, latency: 150, downloadThroughput: 1.6 * 1024 * 1024 / 8, uploadThroughput: 750 * 1024 / 8 });
  }
  const api = [];
  let bytes = 0;
  page.on("response", async (response) => {
    const url = new URL(response.url());
    const timing = response.request().timing();
    const size = Number(response.headers()["content-length"] || 0);
    bytes += size;
    if (url.pathname.startsWith("/api/") && !url.pathname.endsWith("/events/stream")) {
      api.push({ path: url.pathname + url.search, status: response.status(),
                 ttfb_ms: Math.round(timing.responseStart - timing.requestStart), size });
    }
  });
  const started = Date.now();
  await page.goto(path, { waitUntil: "load" });
  // Usable: the page's main heading is on screen (every page has one; the sign-in page would mean a failed login).
  await page.locator("main h1, h1").first().waitFor({ timeout: 60_000 });
  const usable = Date.now() - started;
  await page.waitForTimeout(phone ? 4000 : 2500);  // let the page's follow-up requests finish
  const navigation = await page.evaluate(() => {
    const [entry] = performance.getEntriesByType("navigation");
    const paint = performance.getEntriesByName("first-contentful-paint")[0];
    return { dom_content_loaded_ms: Math.round(entry.domContentLoadedEventEnd), load_ms: Math.round(entry.loadEventEnd),
             first_contentful_paint_ms: paint ? Math.round(paint.startTime) : null,
             transferred_bytes: performance.getEntriesByType("resource").reduce((n, r) => n + (r.transferSize || 0),
                                                                              entry.transferSize || 0) };
  });
  results.push({ name, path, usable_ms: usable, ...navigation, api: api.sort((a, b) => b.ttfb_ms - a.ttfb_ms) });
  await page.close();
}
await browser.close();
console.log(JSON.stringify({ phone, results }, null, 2));
