/** Measure cold page loads: bootstrap + route + explicitly mounted lazy features, static imports only. */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

try {
  const args = process.argv.slice(2);
  const root = args[0] && !args[0].startsWith('--') ? args.shift()
    : fileURLToPath(new URL('../../src/acc/server/static/', import.meta.url));
  let budgetFile = fileURLToPath(new URL('./payload-budgets.json', import.meta.url));
  let reportOnly = false;
  while (args.length) {
    const option = args.shift();
    if (option === '--report') reportOnly = true;
    else if (option === '--budgets' && args[0]) budgetFile = args.shift();
    else throw new Error(`unknown or incomplete option: ${option}`);
  }
  const { pages } = JSON.parse(readFileSync(budgetFile, 'utf8'));
  if (!pages || !Object.keys(pages).length) throw new Error('no page budgets');
  for (const [name, page] of Object.entries(pages)) {
    if (!Array.isArray(page.roots) || page.roots.some((key) => typeof key !== 'string')) throw new Error(`${name}: invalid page roots`);
    for (const kind of ['js', 'css']) {
      for (const metric of ['raw', 'gzip', 'brotli']) {
        if (!Number.isSafeInteger(page[kind]?.[metric]) || page[kind][metric] < 0) throw new Error(`${name}: invalid ${kind} ${metric} baseline`);
      }
    }
  }
  const manifest = JSON.parse(readFileSync(resolve(root, '.vite/manifest.json'), 'utf8'));
  const entries = Object.keys(manifest).filter((key) => manifest[key].isEntry);
  if (!entries.length) throw new Error('build manifest has no entry');
  const measured = new Map();
  function measure(file) {
    if (!measured.has(file)) {
      const content = readFileSync(resolve(root, file));
      measured.set(file, {
        raw: content.length,
        gzip: gzipSync(content, { level: 6 }).length,
        brotli: brotliCompressSync(content, { params: { [constants.BROTLI_PARAM_QUALITY]: 5 } }).length,
      });
    }
    return measured.get(file);
  }
  const result = { pages: {}, largestChunks: [], largestModules: null };
  for (const [name, page] of Object.entries(pages)) {
    const seen = new Set(), files = { js: new Set(), css: new Set() };
    function visit(key) {
      if (seen.has(key)) return;
      seen.add(key);
      const chunk = manifest[key];
      if (!chunk?.file) throw new Error(`missing manifest import: ${key}`);
      if (chunk.file.endsWith('.js')) files.js.add(chunk.file);
      if (chunk.file.endsWith('.css')) files.css.add(chunk.file);
      for (const file of chunk.css ?? []) files.css.add(file);
      for (const dependency of chunk.imports ?? []) visit(dependency);
    }
    [...entries, ...page.roots].forEach(visit);
    if (!files.js.size) throw new Error(`${name}: no initial JavaScript`);
    result.pages[name] = {};
    for (const kind of ['js', 'css']) {
      const total = { raw: 0, gzip: 0, brotli: 0, files: [...files[kind]].sort() };
      for (const file of total.files) {
        const size = measure(file);
        for (const metric of ['raw', 'gzip', 'brotli']) total[metric] += size[metric];
      }
      result.pages[name][kind] = total;
    }
  }
  if (reportOnly) {
    const chunks = new Map();
    for (const [source, chunk] of Object.entries(manifest)) {
      if (/\.(js|css)$/.test(chunk.file)) chunks.set(chunk.file, { source, file: chunk.file, ...measure(chunk.file) });
    }
    result.largestChunks = [...chunks.values()].sort((a, b) => b.raw - a.raw).slice(0, 20);
    const modules = resolve(root, '.vite/module-sizes.json');
    if (existsSync(modules)) result.largestModules = JSON.parse(readFileSync(modules, 'utf8'));
    console.log(JSON.stringify(result, null, 2));
  } else {
    for (const [name, page] of Object.entries(result.pages)) {
      for (const kind of ['js', 'css']) {
        const sizes = page[kind];
        console.log(`${name} ${kind.toUpperCase()}: ${sizes.files.length} files; ${sizes.raw} raw, ${sizes.gzip} gzip, ${sizes.brotli} brotli bytes`);
        for (const metric of ['raw', 'gzip', 'brotli']) {
          const limit = Math.floor(pages[name][kind][metric] * 105 / 100);
          if (sizes[metric] > limit) {
            console.error(`${name} ${kind.toUpperCase()} ${metric} ${sizes[metric]} bytes exceeds ${limit} bytes (baseline +5%)`);
            process.exitCode = 1;
          }
        }
      }
    }
  }
} catch (error) {
  console.error(`Cannot measure initial payload: ${error.message}`);
  process.exitCode = 1;
}
