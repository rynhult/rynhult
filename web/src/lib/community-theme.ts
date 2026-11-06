import baseline from '../../../src/acc/theme_tokens.json' with { type: 'json' };

export type ThemeData = {
  manifest?: { id: string; name: string; version: string; digest: string; author?: string; source?: { description: string }; license?: { spdx: string } };
  palettes: Record<'light' | 'dark', Record<string, string>>;
  presets: Record<'light' | 'dark', Record<string, string>>;
  selection: Record<'light' | 'dark', { background: string; foreground: string }>;
  token?: string; unavailable?: boolean;
};
export type ThemePreference = { choice: { package_id: string; digest: string } | null; revision: number; effective: ThemeData | null; unavailable: boolean };
const fonts = { system: 'system-ui,sans-serif', inter: '"Inter Variable",system-ui,sans-serif', 'jetbrains-mono': '"JetBrains Mono Variable",monospace' };
const presets: Record<string, readonly string[]> = { sans: ['system', 'inter'], mono: ['system', 'jetbrains-mono'], 'font-size': ['default', 'comfortable'], density: ['default', 'comfortable'], radius: ['default', 'soft'], elevation: ['default', 'flat'] };
function color(value: string): string {
  if (typeof value !== 'string' || !/^#[a-f0-9]{6}$/i.test(value)) throw new Error('Theme color is invalid. Built-in Rynhult remains available.');
  return value;
}
function declarations(data: ThemeData, mode: 'light' | 'dark') {
  const values = data.palettes[mode];
  const output = Object.entries(values).map(([key, value]) => {
    if (!Object.hasOwn(baseline[mode], key)) throw new Error('Unsupported theme token.');
    return `--${key}:${color(value)}`;
  });
  const settings = data.presets[mode];
  for (const [key, value] of Object.entries(settings)) if (!presets[key]?.includes(value)) throw new Error('Unsupported theme preset.');
  output.push(`--font-sans:${fonts[(settings.sans ?? 'inter') as keyof typeof fonts]}`);
  output.push(`--font-mono:${settings.mono === 'system' ? 'ui-monospace,monospace' : fonts['jetbrains-mono']}`);
  output.push(`--theme-text-scale:${settings['font-size'] === 'comfortable' ? '1.1' : '1'}`);
  output.push(`--theme-control-padding:${settings.density === 'comfortable' ? '0.875rem' : '0.5rem'}`);
  output.push(`--theme-radius:${settings.radius === 'soft' ? '0.75rem' : '0.375rem'}`);
  if (settings.elevation === 'flat') output.push('--elev-card:none', '--elev-pop:none');
  output.push(`--theme-selection-bg:${color(data.selection[mode].background)}`, `--theme-selection-fg:${color(data.selection[mode].foreground)}`);
  return output.join(';');
}
export function themeCss(data: ThemeData, scope = 'html[data-community-theme]'): string {
  return `${scope}{${declarations(data, 'dark')}}${scope}:not(.dark){${declarations(data, 'light')}}
${scope} body{font-size:calc(13.5px * var(--theme-text-scale));font-family:var(--font-sans)}
${scope} ::selection{background:var(--theme-selection-bg);color:var(--theme-selection-fg)}
${scope} button,${scope} input,${scope} select{border-radius:var(--theme-radius)}
${scope} :disabled{opacity:1;border-style:dashed}
${scope} pre[aria-label="Selectable terminal text"]::selection{background:#abb6ff;color:#10191f}
${scope} button{padding-inline:var(--theme-control-padding)}
@media (pointer:coarse){${scope} button,${scope} input,${scope} select{min-height:44px}}
${scope} :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(forced-colors:active){${scope} ::selection{background:Highlight;color:HighlightText}}`;
}
/** App-owned palette for review/reset chrome. Does not expose theme-controlled selectors or markup. */
export const trustedCss = `.theme-trusted{${Object.entries(baseline.dark).map(([k,v]) => `--${k}:${v}`).join(';')};--theme-selection-bg:${baseline.dark.accent};--theme-selection-fg:${baseline.dark['accent-ink']};font-family:system-ui,sans-serif;font-size:13.5px}
html:not(.dark) .theme-trusted{${Object.entries(baseline.light).map(([k,v]) => `--${k}:${v}`).join(';')};--theme-selection-bg:${baseline.light.accent};--theme-selection-fg:${baseline.light['accent-ink']}}
.theme-trusted button,.theme-trusted input,.theme-trusted select{border-radius:0.375rem;padding-inline:0.5rem;min-height:44px}
.theme-trusted :disabled{opacity:1;border-style:dashed}
.theme-trusted :focus-visible{outline:2px solid var(--accent);outline-offset:2px}`;

export function previewHtml(data: ThemeData, mode: 'light' | 'dark'): string {
  const css = themeCss(data);
  return `<!doctype html><html data-community-theme class="${mode === 'dark' ? 'dark' : ''}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"><style>${css}
body{margin:0;padding:16px;background:var(--bg);color:var(--text);font:14px/1.6 system-ui}section{padding:12px;margin-block:12px;border:1px solid var(--border-input);border-radius:var(--theme-radius);background:var(--panel)}h1{font-size:18px}h2{font-size:16px}button{background:var(--accent);color:var(--accent-ink);border:1px solid var(--border-input);min-height:44px}pre{white-space:pre-wrap;background:#0b0d10;color:#e6e8eb;padding:12px}.muted{color:var(--muted)}.bad{color:var(--bad)}.wait{color:var(--wait)}</style></head><body><h1>Theme preview · synthetic examples</h1><section><h2>Chat</h2><p>Person: Review the example changes.</p><p class="muted">Agent: Ready to review. No live conversation is loaded.</p></section><section><h2>Terminal</h2><pre>example-agent ready\n$ review changes</pre><p>Terminal palette stays built in.</p></section><section><h2>Files</h2><p>example.txt · Unsaved changes</p><code>const ready = true;</code></section><section><h2>Inbox · Approval</h2><p class="wait">Needs you · Example agent on example-host</p><p class="bad">Refused · Folder outside allowed scope</p><button type="button">Example decision (inert)</button></section><section><h2>Settings</h2><p>Account appearance · private to this person</p></section><section><h2>Status board</h2><p>Example session · Idle</p></section></body></html>`;
}
