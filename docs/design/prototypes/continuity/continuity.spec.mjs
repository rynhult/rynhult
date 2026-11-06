import { createRequire } from 'node:module';
import { startServer } from './server.mjs';
const require = createRequire(new URL('../../../../web/package.json', import.meta.url));
const { test, expect } = require('@playwright/test');

async function open(page, approach, guarded = true) {
  const server = await startServer();
  try {
    await page.goto(`${server.url}/?approach=${approach}&guarded=${Number(guarded)}`);
    await page.waitForFunction(() => window.probe?.ready, undefined, { timeout: 10_000 });
    await expect.poll(() => server.stats.connections).toBe(1);
    await page.evaluate(() => window.probe.prepare());
    return server;
  } catch (error) { await page.close(); await server.close(); throw error; }
}

for (const approach of ['stable', 'reparent']) test(`${approach}: raw-move diagnostic captures the missing safeguard`, async ({ page }, info) => {
  const server = await open(page, approach, false);
  try {
    await page.evaluate(() => window.probe.beginComposition());
    const before = await page.evaluate(() => window.probe.snapshot());
    await page.evaluate(() => window.probe.move('column2'));
    const after = await page.evaluate(() => window.probe.snapshot());
    expect(before.selection).toBe('SCROLLBACK-SENTINEL');
    expect(before.preedit).toBe('日本');
    if (approach === 'stable') expect(after.selection).toBe('');
    else {
      expect(after.preedit).toBe('');
      expect(after.blurEvents).toBeGreaterThan(0);
    }
    await info.attach('raw-diagnostic.json', { body: JSON.stringify({ before, after }, null, 2), contentType: 'application/json' });
  } finally { await page.close(); await server.close(); }
});

for (const approach of ['stable', 'reparent']) test(`${approach}: guarded 20 moves retain one terminal, socket and committed composition`, async ({ page }, info) => {
  const server = await open(page, approach);
  try {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Performance.enable');
    const metrics = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(x => [x.name, x.value]));
    const initialMetrics = await metrics();
    const baseline = await page.evaluate(() => window.probe.snapshot());
    expect(baseline.selection).toBe('SCROLLBACK-SENTINEL');
    for (let i = 0; i < 20; i++) {
      const state = await page.evaluate(async i => {
        window.probe.beginComposition();
        await window.probe.move(['column2', 'fill', 'column1'][i % 3]);
        return window.probe.snapshot();
      }, i);
      expect(state.sameTerm).toBe(true);
      expect(state.sameSocket).toBe(true);
      expect(state.sameTextarea).toBe(true);
      expect(state.selection).toBe(baseline.selection);
      expect(state.history[0]).toBe('SCROLLBACK-SENTINEL');
      expect(state.focused).toBe(true);
      expect(state.preedit).toBe('日本');
      expect(state.composing).toBe(true);
      if (approach === 'reparent') expect(state.physicalMoves).toBe(i); // pending until commit
      await page.evaluate(() => window.probe.commit());
      await expect.poll(() => server.stats.inputs.join('')).toBe('日本'.repeat(i + 1));
      expect((await page.evaluate(() => window.probe.snapshot())).selection).toBe(baseline.selection);
      await expect(page.locator('#host')).toBeInViewport();
      const geometry = await page.evaluate(() => {
        const host = document.querySelector('#host').getBoundingClientRect();
        const slot = document.getElementById(window.probe.snapshot().currentSlot).getBoundingClientRect();
        return { x: host.x - slot.x, y: host.y - slot.y, width: host.width - slot.width, height: host.height - slot.height };
      });
      expect(geometry.x).toBeCloseTo(2, 0);
      expect(geometry.y).toBeCloseTo(2, 0);
      expect(geometry.width).toBeCloseTo(-4, 0);
      expect(geometry.height).toBeCloseTo(-4, 0);
    }
    await expect.poll(() => server.stats.resizes.length).toBeGreaterThan(0);
    await page.waitForFunction(() => window.probe.snapshot().outputChunks > 20);
    const beforeInactive = await page.evaluate(() => window.probe.snapshot());
    await page.evaluate(() => window.probe.resizeInactiveSlots());
    const result = await page.evaluate(() => window.probe.snapshot());
    expect(result.resizeSends).toBe(beforeInactive.resizeSends);
    expect(result.fitPasses).toBe(beforeInactive.fitPasses);
    const finalMetrics = await metrics();
    const rendering = { layouts: finalMetrics.LayoutCount - initialMetrics.LayoutCount, styleRecalculations: finalMetrics.RecalcStyleCount - initialMetrics.RecalcStyleCount };
    expect(result.sameParent).toBe(approach === 'stable');
    expect(result.moves).toBe(20);
    expect(result.physicalMoves).toBe(20);
    expect(result.compositionStarts).toBe(20);
    expect(result.compositionEnds).toBe(20);
    expect(result.composing).toBe(false);
    expect(result.history.slice(0, 121)).toEqual(['SCROLLBACK-SENTINEL', ...Array.from({ length: 120 }, (_, n) => `initial-${String(n).padStart(3, '0')}`)]);
    const streamed = result.history.filter(line => line.startsWith('stream-'));
    expect(streamed.length).toBeGreaterThan(20);
    expect(streamed).toEqual(Array.from({ length: streamed.length }, (_, n) => `stream-${String(n).padStart(6, '0')}`));
    expect(server.stats.connections).toBe(1);
    expect(server.stats.closed).toBe(0);
    expect(server.stats.inputs).toEqual(Array(20).fill('日本'));
    expect(server.stats.resizes.every(value => value.source === 'canonical')).toBe(true);
    await expect.poll(() => server.stats.resizes.length).toBe(result.resizeSends);
    expect(result.resizeSends).toBeLessThanOrEqual(21);
    // Count work instead of wall-clock duration on the shared, loaded host.
    expect(result.geometryReads).toBeLessThanOrEqual(21);
    expect(result.fitPasses).toBeLessThanOrEqual(21);
    expect(result.resizeCallbacks).toBeLessThanOrEqual(42);
    expect(result.maxFitsPerFrame).toBeLessThanOrEqual(1);
    // These browser totals include xterm rendering, 20 IME cycles and streamed
    // output. Allow a bounded per-chunk contribution instead of time-based slack.
    expect(rendering.layouts).toBeLessThanOrEqual(240 + 2 * result.outputChunks);
    expect(rendering.styleRecalculations).toBeLessThanOrEqual(240 + 2 * result.outputChunks);
    await info.attach('final-placement.png', { body: await page.screenshot(), contentType: 'image/png' });
    await info.attach('measurements.json', { body: JSON.stringify({ approach, ...result, rendering, server: server.stats }, null, 2), contentType: 'application/json' });
    const { history, ...compact } = result;
    console.log(JSON.stringify({ approach, browser: page.context().browser().version(), ...compact, historyLines: history.length, rendering, connections: server.stats.connections, inputMessages: server.stats.inputs.length, resizeMessages: server.stats.resizes.length }));
  } finally { await page.close(); await server.close(); }
});
