// node --test site/test/site.test.mjs: builds both phases into temporary folders and checks links, privacy and contrast.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

const SITE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PHASES = ["coming-soon", "release"];
const release = JSON.parse(readFileSync(join(SITE, "release.json"), "utf8"));
const builds = {}; // phase → { out, pages, files }
let scratch;

const walk = (dir) => readdirSync(dir).flatMap((n) => statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]);

before(() => {
  scratch = mkdtempSync(join(tmpdir(), "rynhult-site-"));
  for (const phase of PHASES) {
    const out = join(scratch, phase);
    execFileSync(process.execPath, [join(SITE, "build.mjs"), "--phase", phase, "--out", out], { stdio: "pipe" });
    const files = walk(out).map((f) => f.slice(out.length));
    const pages = files.filter((f) => f.endsWith(".html")).map((f) => ({ file: f, html: readFileSync(join(out, f), "utf8") }));
    builds[phase] = { out, pages, files };
  }
});
after(() => rmSync(scratch, { recursive: true, force: true }));

const route = (file) => file.replace(/index\.html$/, "");
const target = (out, href) => {
  const path = decodeURIComponent(href.split(/[?#]/)[0]);
  return path.endsWith("/") ? join(out, path, "index.html") : join(out, path);
};

test("coming-soon builds only the landing page, security, licence and 404", () => {
  const { files } = builds["coming-soon"];
  assert.deepEqual(builds["coming-soon"].pages.map((p) => p.file).sort(),
    ["/404.html", "/index.html", "/license/index.html", "/security/index.html"]);
  assert.ok(!files.some((f) => /^\/(docs|install|download)\//.test(f)), "no docs, install or download output");
  const sitemap = readFileSync(join(builds["coming-soon"].out, "sitemap.xml"), "utf8");
  assert.deepEqual([...sitemap.matchAll(/<loc>https:\/\/rynhult\.dev([^<]*)<\/loc>/g)].map((m) => m[1]).sort(), ["/", "/license/", "/security/"]);
});

test("coming-soon points people at GitHub and says a release candidate is coming", () => {
  const home = builds["coming-soon"].pages.find((p) => p.file === "/index.html").html;
  assert.match(home, new RegExp(`<a class="button primary" href="${release.follow}">Follow on GitHub</a>`));
  assert.match(home, /<p class="release-note"><strong>A public release candidate is coming soon\.<\/strong>/);
  assert.ok(home.indexOf("release-note") < home.indexOf("Follow on GitHub"), "the notice sits above the call to action");
  for (const { file, html } of builds["coming-soon"].pages) {
    assert.doesNotMatch(html, /href="\/(docs|install|download)\b/, `${file} links to a page that isn't built`);
    assert.doesNotMatch(html, /\{\{/, `${file} has an unfilled placeholder`);
    assert.match(html, new RegExp(`<a class="quiet-link" href="${release.follow}">GitHub</a>`), `${file}: GitHub in the site nav`);
  }
});

test("coming-soon doesn't claim Rynhult is open source yet; the release phase does", () => {
  const soon = "Rynhult will be open source under the <a href=\"/license/\">MIT licence</a> when the release candidate ships.";
  for (const { file, html } of builds["coming-soon"].pages) {
    const footer = html.slice(html.indexOf('<footer '));
    assert.ok(footer.includes(soon), `${file}: coming-soon footer wording`);
    assert.doesNotMatch(html, /is open source|MIT-licensed/, `${file}: claims it is open source already`);
  }
  for (const { file, html } of builds.release.pages) {
    assert.ok(html.includes('Self-hosted, open source under the MIT licence.'), `${file}: release footer wording`);
  }
});

test("release builds the full site with the docs", () => {
  assert.ok(builds.release.pages.length > 60);
  for (const page of ["/install/index.html", "/download/index.html", "/docs/index.html"]) assert.ok(builds.release.files.includes(page), page);
});

test("both phases publish the repository's exact public release key and keep the strict CSP", () => {
  const key = readFileSync(join(SITE, "../keys/rynhult-release-key.asc"), "utf8");
  for (const phase of PHASES) {
    assert.equal(readFileSync(join(builds[phase].out, "keys/rynhult-release-key.asc"), "utf8"), key);
    const security = builds[phase].pages.find((p) => p.file === "/security/index.html").html;
    assert.match(security, /href="\/keys\/rynhult-release-key\.asc"/);
    assert.match(security, /default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'/);
  }
});

test("every page has the basics: language, one h1, title, description, skip link and main landmark", () => {
  for (const phase of PHASES) {
    for (const { file, html } of builds[phase].pages) {
      const where = `${phase} ${route(file)}`;
      assert.match(html, /<html lang="en">/, where);
      assert.equal((html.match(/<h1[ >]/g) ?? []).length, 1, `${where}: one h1`);
      assert.match(html, /<title>[^<]+<\/title>/, where);
      assert.match(html, /<meta name="description" content="[^"]{20,}">/, where);
      assert.match(html, /<a class="skip" href="#main">/, where);
      assert.match(html, /<main id="main" tabindex="-1">/, where);
    }
  }
});

test("every internal link, image and anchor resolves inside the built site", () => {
  for (const phase of PHASES) {
    const { out, pages } = builds[phase];
    const ids = new Map(pages.map(({ file, html }) => [join(out, file), new Set([...html.matchAll(/ id="([^"]+)"/g)].map((m) => m[1]))]));
    for (const { file, html } of pages) {
      const where = `${phase} ${route(file)}`;
      for (const [, attr, value] of html.matchAll(/ (href|src)="([^"]*)"/g)) {
        if (/^(https?:|mailto:)/.test(value)) continue;
        if (value.startsWith("#")) {
          assert.ok(ids.get(join(out, file)).has(decodeURIComponent(value.slice(1))), `${where}: missing #${value.slice(1)}`);
          continue;
        }
        assert.ok(value.startsWith("/"), `${where}: relative ${attr} ${value}`);
        const dest = target(out, value);
        assert.ok(existsSync(dest), `${where}: ${attr} ${value} does not exist`);
        const anchor = value.split("#")[1];
        if (anchor && dest.endsWith(".html")) assert.ok(ids.get(dest)?.has(decodeURIComponent(anchor)), `${where}: ${value} anchor`);
      }
    }
  }
});

test("nothing loads from another server: no scripts, no external styles, images or fonts", () => {
  for (const phase of PHASES) {
    const { out, pages } = builds[phase];
    for (const { file, html } of pages) {
      assert.doesNotMatch(html, /<script/i, route(file));
      const resources = html.replace(/<link rel="canonical" [^>]*>/, ""); // an address, not something the browser loads
      assert.doesNotMatch(resources, /<(img|link|iframe|source|video|audio|object|embed)\b[^>]*(src|href|data)="(https?:)?\/\//i, route(file));
      assert.doesNotMatch(html, /\bstyle="/i, `${route(file)}: inline style would break the CSP`);
    }
    const css = readdirSync(out).filter((n) => n.endsWith(".css")).map((n) => readFileSync(join(out, n), "utf8")).join("");
    assert.doesNotMatch(css, /@import/);
    for (const [, url] of css.matchAll(/url\(([^)]*)\)/g)) {
      assert.match(url, /^\/fonts\/[\w.-]+\.woff2$/, `stylesheet loads ${url}`);
      assert.ok(existsSync(join(out, url)), `${url} is in the build`);
    }
  }
});

test("the only external links are to GitHub or written in the docs", () => {
  for (const { file, html } of builds["coming-soon"].pages) {
    for (const [, url] of html.matchAll(/<a [^>]*href="(https?:[^"]+)"/g)) assert.equal(url, release.follow, `${file}: ${url}`);
  }
});

test("the self-hosted font stays small and is listed in the third-party notices", () => {
  const font = join(SITE, "assets/fonts/noto-sans-latin-wght.woff2");
  assert.ok(statSync(font).size < 48 * 1024, "font subset under 48 KB");
  const notices = readFileSync(join(SITE, "../THIRD_PARTY_NOTICES.md"), "utf8");
  assert.match(notices, /Noto Sans/);
  assert.match(notices, /SIL Open Font License/);
});

// WCAG 2.x relative luminance contrast.
const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};

test("colour tokens meet WCAG AA in light and dark", () => {
  const block = text => Object.fromEntries([...text.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
  const roots = name => [...readFileSync(join(SITE, 'assets', name), 'utf8').matchAll(/:root\s*\{([^}]+)\}/g)].map(m => block(m[1]));
  const tokens = roots('tokens.css'), product = roots('site.css');
  const light = {...tokens[0], ...product[0]};
  const dark = {...light, ...tokens[1], ...product[1]};
  const text = [["ink", "bg"], ["muted", "bg"], ["link", "bg"], ["display", "bg"], ["ink", "surface"], ["muted", "surface"],
    ["link", "surface"], ["ink", "sunk"], ["preview-ink", "preview-bg"], ["later-ink", "later-bg"], ["ok", "bg"], ["bg", "ink"]];
  const ui = [["focus", "bg"], ["mark", "bg"], ["focus", "surface"]];
  for (const [name, tokens] of [["light", light], ["dark", dark]]) {
    const value = (t) => { let v = tokens[t]; while (v?.startsWith("var(--")) v = tokens[v.slice(6, -1)]; return v; };
    for (const [fg, bg] of text) assert.ok(ratio(value(fg), value(bg)) >= 4.5, `${name}: ${fg} on ${bg} is ${ratio(value(fg), value(bg)).toFixed(2)}`);
    for (const [fg, bg] of ui) assert.ok(ratio(value(fg), value(bg)) >= 3, `${name}: ${fg} on ${bg} is ${ratio(value(fg), value(bg)).toFixed(2)}`);
  }
});

test("before a release, no page claims a version is available", () => {
  if (release.version) return;
  const download = builds.release.pages.find((p) => p.file === "/download/index.html").html;
  assert.match(download, /Nothing to download yet/);
  for (const phase of PHASES) {
    for (const { file, html } of builds[phase].pages) assert.doesNotMatch(html, /\/releases\/download\//, `${phase} ${file}`);
  }
});

test('product-only build has stable CSS and no landing-owned paths', () => {
  const out = join(scratch, 'product');
  execFileSync(process.execPath, [join(SITE, 'build.mjs'), '--product-only', '--phase', 'release', '--out', out]);
  const files = walk(out).map(f => f.slice(out.length + 1));
  assert.ok(files.includes('site.css'));
  assert.deepEqual(JSON.parse(readFileSync(join(out, 'release.json'))), {...release, phase:'release'});
  assert.ok(files.includes('404.html') && files.includes('og.png') && files.includes('keys/rynhult-release-key.asc'));
  assert.ok(!files.some(f => f === 'index.html' || f.startsWith('fonts/') || /^(favicon|icon-)/.test(f) || f === 'og-image.png'));
  const html = readFileSync(join(out, 'docs/index.html'), 'utf8');
  assert.match(html, /href="\/site\.css\?v=[0-9a-f]+"/);
  const header = html.match(/<header[\s\S]*?<\/header>/)[0];
  assert.match(header, /class="wrap cluster"/);
  assert.match(header, /class="lockup"/);
  assert.match(header, /class="mark"/);
  assert.deepEqual([...header.matchAll(/class="quiet-link" href="[^"]+">([^<]+)/g)].map(m => m[1]), ['Docs','GitHub']);
  const footer = html.match(/<footer[\s\S]*?<\/footer>/)[0];
  assert.match(footer, /Rynhult \(RIN-hult\)/);
  assert.match(footer, /class="visually-hidden"/);
  for (const path of ['/docs/', '/install/', '/download/', '/security/', '/license/']) assert.ok(footer.includes(`href="${path}"`));
});
