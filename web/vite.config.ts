import { execFileSync } from "node:child_process";
import { relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { xtermCompositionFix } from "./scripts/xterm-composition.mjs";

let buildDirectory: string;
let sourceDirectory: string;

export default defineConfig({
  plugins: [xtermCompositionFix(), react(), tailwindcss(), {
    name: "initial-javascript-budget",
    apply: "build",
    configResolved(config) { buildDirectory = resolve(config.root, config.build.outDir); sourceDirectory = config.root; },
    generateBundle(_options, bundle) {
      const modules = Object.values(bundle).flatMap((chunk) => chunk.type !== "chunk" ? [] :
        Object.entries(chunk.modules).map(([id, module]) => ({
          source: id.includes("/node_modules/") ? `node_modules/${id.split("/node_modules/").pop()}` : relative(sourceDirectory, id),
          renderedLength: module.renderedLength,
          chunk: chunk.fileName,
        })));
      this.emitFile({ type: "asset", fileName: ".vite/module-sizes.json",
        source: JSON.stringify(modules.sort((a, b) => b.renderedLength - a.renderedLength).slice(0, 25), null, 2) });
    },
    closeBundle() {
      execFileSync(process.execPath, [fileURLToPath(new URL("./scripts/check-bundle-size.mjs", import.meta.url)), buildDirectory], { stdio: "inherit" });
    },
  }],
  build: {
    manifest: true, outDir: "../src/acc/server/static", emptyOutDir: true, sourcemap: false,
    // Fonts are always files: the CSP (font-src 'self') blocks data: URLs, and a small inlined subset showed a console
    // error on every page (UX review R11).
    assetsInlineLimit: (file) => (/\.(woff2?|ttf|otf)$/.test(file) ? false : undefined),
  },
  // Keep the same reviewed composition fix in development and production.
  optimizeDeps: { exclude: ["@xterm/xterm"] },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8740", ws: true } } },
});
