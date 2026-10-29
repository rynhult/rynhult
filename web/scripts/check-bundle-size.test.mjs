import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { spawnSync } from 'node:child_process';

const script = new URL('./check-bundle-size.mjs', import.meta.url);
const generous = { raw: 2_000_000, gzip: 250 * 1024, brotli: 2_000_000 };
const budgets = (roots = [], js = generous, css = generous) => ({ pages: { test: { roots, js: { ...js }, css: { ...css } } } });
function fixture(manifest, files, run, limits = budgets()) {
  const dir = mkdtempSync(join(tmpdir(), 'acc-size-'));
  try {
    mkdirSync(join(dir, '.vite'));
    writeFileSync(join(dir, '.vite/manifest.json'), JSON.stringify(manifest));
    writeFileSync(join(dir, 'budgets.json'), JSON.stringify(limits));
    for (const [name, content] of Object.entries(files)) writeFileSync(join(dir, name), content);
    run(dir);
  } finally { rmSync(dir, { recursive: true, force: true }); }
}
const check = (dir, ...args) => spawnSync(process.execPath,
  [script.pathname, dir, '--budgets', join(dir, 'budgets.json'), ...args], { encoding: 'utf8' });

test('counts shared static imports once, handles cycles and excludes lazy routes', () => {
  fixture({
    'index.html': { file: 'entry.js', isEntry: true, imports: ['shared', 'other'], dynamicImports: ['lazy'] },
    shared: { file: 'shared.js', imports: ['other'] },
    other: { file: 'other.js', imports: ['shared'] },
    lazy: { file: 'lazy.js' },
  }, { 'entry.js': 'entry', 'shared.js': 'shared', 'other.js': 'other', 'lazy.js': randomBytes(400_000) }, (dir) => {
    const result = check(dir);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /test JS: 3 files/);
  });
});

test('rejects a large transitive initial payload even when each file is under budget', () => {
  fixture({ 'index.html': { file: 'entry.js', isEntry: true, imports: ['shared'] }, shared: { file: 'shared.js' } },
    { 'entry.js': randomBytes(160_000), 'shared.js': randomBytes(160_000) }, (dir) => {
      const result = check(dir);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /test JS gzip .*exceeds/);
    });
});

test('measures compressed bytes rather than raw size', () => {
  fixture({ 'index.html': { file: 'entry.js', isEntry: true } }, { 'entry.js': 'x'.repeat(400_000) }, (dir) => {
    const result = check(dir);
    assert.equal(result.status, 0, result.stderr);
  });
});

for (const problem of ['no entry', 'missing import', 'missing file']) {
  test(`fails closed for ${problem}`, () => {
    const entry = { file: 'entry.js', isEntry: true, imports: problem === 'missing import' ? ['absent'] : [] };
    fixture(problem === 'no entry' ? {} : { 'index.html': entry },
      problem === 'missing file' ? {} : { 'entry.js': 'entry' }, (dir) => {
        const result = check(dir);
        assert.equal(result.status, 1);
        assert.match(result.stderr, /Cannot measure initial payload/);
      });
  });
}

test('measures the requested lazy route and shared CSS, but not its unopened features', () => {
  fixture({
    'index.html': { file: 'entry.js', isEntry: true, imports: ['shared'], css: ['shared.css'], dynamicImports: ['login', 'board'] },
    shared: { file: 'shared.js', css: ['shared.css'] },
    login: { file: 'login.js', imports: ['shared'], css: ['login.css'], dynamicImports: ['editor'] },
    board: { file: 'board.js' },
    editor: { file: 'editor.js' },
  }, { 'entry.js': 'entry', 'shared.js': 'shared', 'login.js': 'login', 'board.js': 'board',
    'editor.js': 'x'.repeat(400_000), 'shared.css': 'body{}', 'login.css': '.login{}' }, (dir) => {
    const result = check(dir, '--report');
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /"pages":/);
    const page = JSON.parse(result.stdout).pages.test;
    assert.deepEqual(page.js.files.sort(), ['entry.js', 'login.js', 'shared.js']);
    assert.deepEqual(page.css.files.sort(), ['login.css', 'shared.css']);
    assert.deepEqual([page.js.raw, page.js.gzip, page.js.brotli], [16, 76, 28]);
    assert.deepEqual([page.css.raw, page.css.gzip, page.css.brotli], [14, 54, 22]);
  }, budgets(['login']));
});

test('counts automatically mounted lazy features as explicit page roots', () => {
  fixture({
    'index.html': { file: 'entry.js', isEntry: true, dynamicImports: ['session'] },
    session: { file: 'session.js', dynamicImports: ['terminal'] },
    terminal: { file: 'terminal.js', imports: ['shared'] },
    shared: { file: 'shared.js' },
  }, { 'entry.js': 'entry', 'session.js': 'session', 'terminal.js': 'terminal', 'shared.js': 'shared' }, (dir) => {
    const result = check(dir, '--report');
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /"pages":/);
    assert.deepEqual(JSON.parse(result.stdout).pages.test.js.files.sort(), ['entry.js', 'session.js', 'shared.js', 'terminal.js']);
  }, budgets(['session', 'terminal']));
});

for (const [bytes, status] of [[1050, 0], [1051, 1]]) {
  test(`enforces the five-percent raw JS boundary at ${bytes} bytes`, () => {
    fixture({ 'index.html': { file: 'entry.js', isEntry: true } }, { 'entry.js': 'x'.repeat(bytes) }, (dir) => {
      const result = check(dir);
      assert.equal(result.status, status, result.stdout + result.stderr);
      if (status) assert.match(result.stderr, /test JS raw .*exceeds/);
    }, budgets([], { ...generous, raw: 1000 }));
  });
}

for (const [kind, metric] of [['js', 'gzip'], ['js', 'brotli'], ['css', 'raw'], ['css', 'gzip'], ['css', 'brotli']]) {
  test(`enforces the page's ${kind} ${metric} budget independently`, () => {
    const limits = budgets();
    limits.pages.test[kind] = { ...generous, [metric]: 1 };
    fixture({ 'index.html': { file: 'entry.js', isEntry: true, css: ['style.css'] } },
      { 'entry.js': 'entry', 'style.css': 'body{}' }, (dir) => {
        const result = check(dir);
        assert.equal(result.status, 1, result.stdout + result.stderr);
        assert.match(result.stderr, new RegExp(`test ${kind.toUpperCase()} ${metric} .*exceeds`));
      }, limits);
  });
}

for (const problem of ['route', 'CSS', 'budget metric', 'pages']) {
  test(`fails closed for missing ${problem}`, () => {
    const limits = budgets(problem === 'route' ? ['absent'] : []);
    if (problem === 'budget metric') delete limits.pages.test.js.brotli;
    if (problem === 'pages') limits.pages = {};
    fixture({ 'index.html': { file: 'entry.js', isEntry: true, css: problem === 'CSS' ? ['absent.css'] : [] } },
      { 'entry.js': 'entry' }, (dir) => {
        const result = check(dir);
        assert.equal(result.status, 1, result.stdout + result.stderr);
        assert.match(result.stderr, /Cannot measure initial payload/);
      }, limits);
  });
}
