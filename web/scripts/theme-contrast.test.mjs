import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const tokens = selector => Object.fromEntries([...css.match(new RegExp(`${selector} \\{([^}]+)}`))[1].matchAll(/--([\w-]+):\s*(#[\da-f]{6})/gi)].map(([, k, v]) => [k, v]));
const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
const luminance = color => color.map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
const ratio = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
const mix = (a, b, opacity) => a.map((v, i) => v * opacity + b[i] * (1 - opacity));
const baseline = {
  dark: { surfaces: ['#09090b', '#111114', '#18181c', '#1f1f24'], ok: '#4cc38a', wait: '#e5a83b', bad: '#f06a6a', info: '#6cb4ff', idle: '#8e95a3' },
  light: { surfaces: ['#f7f7f8', '#ffffff', '#f2f2f4', '#e9e9ed'], ok: '#0f7a49', wait: '#955300', bad: '#c52828', info: '#1a63c0', idle: '#64748b' },
};
for (const [theme, selector] of [['dark', ':root'], ['light', 'html:not\\(.dark\\)']]) {
  const t = tokens(selector);
  const surfaces = ['bg', 'panel', 'panel-2', 'panel-3'];
  test(`${theme}: body text, status badges, field boundaries and primary actions meet AA`, () => {
    for (const surface of surfaces) {
      for (const text of ['text', 'muted', 'faint', 'accent', 'accent-strong', 'ok', 'wait', 'bad', 'info', 'idle']) {
        const fg = rgb(t[text]), bg = rgb(t[surface]);
        assert.ok(ratio(fg, bg) >= 4.5, `${text} on ${surface}: ${ratio(fg, bg)}`);
        if (['accent', 'ok', 'wait', 'bad', 'info'].includes(text)) {
          const tinted = mix(fg, bg, .12);
          assert.ok(ratio(fg, tinted) >= 4.5, `${text} on 12% badge over ${surface}: ${ratio(fg, tinted)}`);
        }
      }
      assert.ok(ratio(rgb(t['border-input']), rgb(t[surface])) >= 3, `field boundary on ${surface}`);
      assert.ok(ratio(rgb(t.accent), rgb(t[surface])) >= 3, `focus on ${surface}`);
    }
    for (const status of ['bad', 'wait']) {
      assert.ok(ratio(rgb(t['status-ink'] ?? '#000000'), rgb(t[status])) >= 4.5, `Inbox count ink on ${status}`);
    }
    for (const brightness of [1, 1.1]) {
      const brighten = color => rgb(color).map(v => Math.min(1, v * brightness));
      assert.ok(ratio(brighten(t['accent-ink']), brighten(t.accent)) >= 4.5, `primary action at brightness ${brightness}`);
    }
  });
  test(`${theme}: status contrast never decreases from stage 1, including tinted surfaces`, () => {
    const old = baseline[theme];
    for (const status of ['ok', 'wait', 'bad', 'info', 'idle']) for (const [i, surface] of surfaces.entries()) for (const opacity of [0, .1, .12]) {
      const now = ratio(rgb(t[status]), mix(rgb(t[status]), rgb(t[surface]), opacity));
      const before = ratio(rgb(old[status]), mix(rgb(old[status]), rgb(old.surfaces[i]), opacity));
      assert.ok(now >= before, `${status} on ${surface} at ${opacity}: ${now} < stage 1 ${before}`);
    }
  });
}

test('installed-app splash and browser chrome match the dark app surface', () => {
  const manifest = JSON.parse(readFileSync(new URL('../public/manifest.webmanifest', import.meta.url), 'utf8'));
  const dark = tokens(':root');
  assert.equal(manifest.background_color.toLowerCase(), dark.bg);
  assert.equal(manifest.theme_color.toLowerCase(), dark.bg);
});
