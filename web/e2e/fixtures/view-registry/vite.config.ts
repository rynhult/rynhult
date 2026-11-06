import { defineConfig } from 'vite';
import { xtermCompositionFix } from '../../../scripts/xterm-composition.mjs';
// Static fixture only: no API proxy, app server, login, or real sessions.
export default defineConfig({
  plugins: [xtermCompositionFix()], optimizeDeps: { exclude: ['@xterm/xterm'] },
  // Trace HTML is written under the web root by the normal suite. It must not
  // reload this fixture or reconnect its socket while a test is in progress.
  server: { hmr: false, watch: { ignored: ['**/e2e/.results/**'] } },
});
