#!/usr/bin/env node
// Builds rynhult.dev into site/dist: the hand-written pages in site/pages and the public wiki (docs/wiki) under /docs/.
// Two phases (release.json "phase", or --phase): "coming-soon" builds only the landing page, security, licence and
// 404, with no /docs, install or download pages; "release" builds everything.
// No new dependencies: Markdown goes through micromark + GFM, the parser the app's Guide already ships
// (locked in web/package-lock.json), so run `npm ci` in web/ first. Usage: node site/build.mjs [--out DIR]
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, posix, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SITE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(SITE, "..");
const WIKI = join(ROOT, "docs/wiki");
const ORIGIN = "https://rynhult.dev";
const outArg = process.argv.indexOf("--out");
const OUT = resolve(outArg > 0 ? process.argv[outArg + 1] : join(SITE, "dist"));

if (existsSync(OUT)) {
  if (ROOT.startsWith(OUT) || !existsSync(join(OUT, "sitemap.xml"))) throw new Error(`refusing to clear ${OUT}: not a previous site build`);
  rmSync(OUT, { recursive: true });
}

const web = (name) => import(pathToFileURL(join(ROOT, "web/node_modules", name, "index.js")).href);
const { micromark } = await web("micromark");
const { gfm, gfmHtml } = await web("micromark-extension-gfm");

const problems = [];
const escape = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const decode = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'")
  .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n)).replace(/&amp;/g, "&");
const write = (path, content) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, content); };

// --- Shared chrome -------------------------------------------------------------------------------------------------
// Landing chrome copied from rynhult-site scripts/page.js and partials at 3bec36dc9832a836b00c326a996c8893fb52fcb1.
const logo = ['logo-lockup.svg', 'logo-mark.svg'].map(name => readFileSync(join(SITE, 'assets', name), 'utf8')).join('\n');
const css = Buffer.from(['tokens.css', 'base.css', 'site.css'].map(name => readFileSync(join(SITE, 'assets', name), 'utf8')).join('\n'));
const cssName = 'site.css';
const cssHash = createHash('sha256').update(css).digest('hex').slice(0, 10);
const PRODUCT_ONLY = process.argv.includes('--product-only');
const release = JSON.parse(readFileSync(join(SITE, "release.json"), "utf8"));
const phaseArg = process.argv.indexOf("--phase");
const PHASE = phaseArg > 0 ? process.argv[phaseArg + 1] : release.phase;
if (!["coming-soon", "release"].includes(PHASE)) throw new Error(`unknown phase ${PHASE}: use coming-soon or release`);
const SOON = PHASE === "coming-soon";
if (!/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/.test(release.follow)) throw new Error("release.json follow must be a GitHub repository URL");

const NAV = SOON
  ? [["/#what", "Features"], ["/security/", "Security"], [release.follow, "GitHub"]]
  : [["/docs/", "Docs"], [release.follow, "GitHub"]];
const FOOTER = {"links": [{"id": "docs", "label": "Docs", "href": "/docs/"}, {"id": "install", "label": "Install", "href": "/install/"}, {"id": "download", "label": "Download", "href": "/download/"}, {"id": "security", "label": "Security", "title": "Report a vulnerability", "href": "/security/"}, {"id": "licence", "label": "Licence", "href": "/license/"}, {"id": "github", "label": "GitHub", "href": "{{release.follow}}"}, {"id": "youtube", "label": "@rynhult", "href": "https://www.youtube.com/@rynhult"}], "name": "Rynhult (RIN-hult)", "line": "Self-hosted, open source under the MIT licence."};

function layout({ path, title, description, body, main = "main" }) {
  const full = path === "/" ? "Rynhult: self-hosted control for coding agents" : `${title} · Rynhult`;
  const nav = NAV.map(([href, label]) => `<a class="quiet-link" href="${escape(href)}">${label}</a>`).join('\n      ');
  const footerLinks = (SOON ? [FOOTER.links[4], FOOTER.links[3], FOOTER.links[5]] : FOOTER.links).map(link => ({...link, href: link.href.replace('{{release.follow}}', release.follow)}));
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="strict-origin-when-cross-origin">
<title>${escape(full)}</title>
<meta name="description" content="${escape(description)}">
<link rel="canonical" href="${ORIGIN}${path}">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#F0F3F3" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#10191F" media="(prefers-color-scheme: dark)">
<meta property="og:type" content="website">
<meta property="og:title" content="${escape(full)}">
<meta property="og:description" content="${escape(description)}">
<meta property="og:url" content="${ORIGIN}${path}">
<meta property="og:image" content="${ORIGIN}/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="preload" href="/fonts/noto-sans-latin-wght.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/${cssName}?v=${cssHash}">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site-header">
    <div class="wrap cluster">
      <a class="logo" href="/" aria-label="Rynhult home">
        ${logo}
      </a>
      <nav class="cluster" aria-label="Site">
      ${nav}
      </nav>
    </div>
</header>
<${main} id="main" tabindex="-1">
${body}
</${main}>
<footer class="site-footer section">
    <div class="wrap">
      <nav aria-label="Footer">
        <ul class="cluster links">
          ${footerLinks.map(link => `<li><a href="${escape(link.href)}"${link.title ? ` title="${escape(link.title)}"` : ''}>${escape(link.label)}${link.title ? `<span class="visually-hidden"> (${escape(link.title)})</span>` : ''}</a></li>`).join('\n          ')}
        </ul>
      </nav>
      <p class="small colophon"><strong>${escape(FOOTER.name)}</strong> ${SOON ? 'Rynhult will be open source under the <a href="/license/">MIT licence</a> when the release candidate ships.' : escape(FOOTER.line)}</p>
    </div>
  </footer>
</body>
</html>
`;
}

// --- Hand-written pages --------------------------------------------------------------------------------------------
// Each file in site/pages starts with a JSON comment: <!--{"path": "/", "title": "...", "description": "..."}-->
// and may use {{release.*}}, {{follow}} and {{licence}} placeholders. A page that exists only in the release phase
// says so with "phases": ["release"]; {{#soon}}…{{/soon}} and {{#release}}…{{/release}} keep text for one phase.
function fill(html) {
  const version = release.version;
  html = html.replace(/\{\{#(soon|release)\}\}([\s\S]*?)\{\{\/\1\}\}/g, (_, which, inner) => ((which === "soon") === SOON ? inner : ""));
  const blocks = {
    "release.note": SOON
      ? `<strong>A public release candidate is coming soon.</strong> <span class="nowrap">${escape(release.planned)}</span> will be the first public release of Rynhult. Follow the project on GitHub to hear when it ships.`
      : version
      ? `Current release: <a href="/download/">${escape(version)}</a>, a ${escape(release.channel)}.`
      : `The first public release candidate, <span class="nowrap">${escape(release.planned)}</span>, is in preparation. Install and download links appear here when it ships.`,
    follow: escape(release.follow),
    "release.downloads": downloads(),
    licence: escape(readFileSync(join(ROOT, "LICENSE"), "utf8").trim()),
  };
  return html.replace(/\{\{([\w.]+)\}\}/g, (_, key) => {
    if (!(key in blocks)) throw new Error(`unknown placeholder {{${key}}}`);
    return blocks[key];
  });
}

function downloads() {
  if (!release.version) {
    return `<div class="notice"><p><strong>Nothing to download yet.</strong> ${escape(release.planned)} is the first public release candidate. Its source archive and SHA-256 checksums will be listed here when it is published.</p></div>`;
  }
  const rows = release.assets.map((a) => `<tr><td><a href="${escape(a.url)}">${escape(a.name)}</a></td><td>${escape(a.description)}</td><td><div class="download-checksum" tabindex="0" role="region" aria-label="SHA-256 for ${escape(a.name)}"><code>${escape(a.sha256)}</code></div></td></tr>`).join("");
  return `<p id="download-table-help">On smaller screens, scroll the table to see all columns.</p><div class="table-scroll" tabindex="0" role="region" aria-label="Release files" aria-describedby="download-table-help"><table class="release-downloads"><colgroup><col class="download-file"><col class="download-description"><col class="download-hash"></colgroup><thead><tr><th scope="col">File</th><th scope="col">What it is</th><th scope="col">SHA-256</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

const sitemap = [];
for (const name of readdirSync(join(SITE, "pages")).filter((n) => n.endsWith(".html")).sort()) {
  const source = readFileSync(join(SITE, "pages", name), "utf8");
  const head = source.match(/^<!--(\{.*?\})-->\n/s);
  if (!head) throw new Error(`${name}: missing JSON header comment`);
  const meta = JSON.parse(head[1]);
  if (!(meta.phases ?? ["coming-soon", "release"]).includes(PHASE)) continue;
  if (PRODUCT_ONLY && meta.path === "/") { sitemap.push("/"); continue; }
  const body = fill(source.slice(head[0].length));
  const file = meta.path === "/404" ? "404.html" : join(meta.path, "index.html");
  write(join(OUT, file), layout({ ...meta, body }));
  if (meta.path !== "/404") sitemap.push(meta.path);
}

// --- Wiki → /docs/ -------------------------------------------------------------------------------------------------
// Azure DevOps "code as wiki" layout: Name.md with a peer folder Name/ for subpages, .order for sequence,
// /.attachments for images, absolute links like /User-Guide/Sessions#anchor. Routes become /docs/<route>/.
const pages = new Map(); // route → { file, title, order }
function collect(folder, prefix) {
  const order = existsSync(join(folder, ".order")) ? readFileSync(join(folder, ".order"), "utf8").split("\n").map((l) => l.trim()).filter(Boolean) : [];
  const names = readdirSync(folder).filter((n) => n.endsWith(".md")).map((n) => n.slice(0, -3));
  const sorted = [...order.filter((n) => names.includes(n)), ...names.filter((n) => !order.includes(n)).sort()];
  return sorted.map((name) => {
    const route = prefix ? `${prefix}/${name}` : name;
    const file = join(folder, `${name}.md`);
    const text = readFileSync(file, "utf8");
    const title = (text.match(/^#\s+(.+)$/m)?.[1] ?? name.replace(/-/g, " ")).replace(/[*_`]/g, "");
    pages.set(route, { file, title });
    const children = existsSync(join(folder, name)) && statSync(join(folder, name)).isDirectory() ? collect(join(folder, name), route) : [];
    return { route, title, children };
  });
}
const tree = SOON ? [] : collect(WIKI, ""); // the wiki isn't published before the release

const docsHref = (route) => (route === "Home" ? "/docs/" : `/docs/${route.split("/").map(encodeURIComponent).join("/")}/`);
const slug = (text) => text.toLowerCase().replace(/[^\p{L}\p{N}_ -]/gu, "").replace(/ /g, "-");

// The same GitHub-style ids as the app's Guide (web/src/components/Wiki.tsx remarkHeadings), so shared links agree.
function headingIds(html) {
  const seen = new Map();
  const ids = new Set();
  const out = html.replace(/<h([1-6])>([\s\S]*?)<\/h\1>/g, (_, level, inner) => {
    const base = slug(decode(inner.replace(/<[^>]+>/g, "")));
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    const id = count ? `${base}-${count}` : base;
    ids.add(id);
    return `<h${level} id="${id}">${inner}</h${level}>`;
  });
  return { html: out, ids };
}

const attachments = new Set();
const anchorChecks = []; // [from route, to route, anchor]

function rewrite(html, route) {
  const base = posix.dirname(`/${route}.md`);
  html = html.replace(/<img src="([^"]*)" alt="([^"]*)"( title="[^"]*")? \/>/g, (all, src, alt, title = "") => {
    const path = posix.resolve(base, decode(src));
    if (!path.startsWith("/.attachments/")) { problems.push(`${route}: image outside /.attachments: ${src}`); return all; }
    const name = path.slice("/.attachments/".length);
    const file = join(WIKI, ".attachments", name);
    if (!existsSync(file)) { problems.push(`${route}: missing image ${src}`); return all; }
    attachments.add(name);
    const size = pngSize(file);
    const dims = size ? ` width="${size[0]}" height="${size[1]}"` : "";
    return `<img src="/docs/media/${escape(name)}" alt="${alt}"${title}${dims} loading="lazy" decoding="async">`;
  });
  return html.replace(/<a href="([^"]*)"/g, (all, href) => {
    const raw = decode(href);
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return all;
    const [target, anchor = ""] = raw.split("#");
    if (!target) { if (anchor) anchorChecks.push([route, route, anchor]); return all; }
    const path = posix.resolve(base, target).replace(/\.md$/, "").replace(/\/$/, "").slice(1) || "Home";
    if (!pages.has(path)) { problems.push(`${route}: link to a page outside the public wiki: ${raw}`); return all; }
    if (anchor) anchorChecks.push([route, path, anchor]);
    return `<a href="${escape(docsHref(path) + (anchor ? `#${anchor}` : ""))}"`;
  });
}

function pngSize(file) {
  const head = readFileSync(file).subarray(0, 24);
  return head.toString("latin1", 12, 16) === "IHDR" ? [head.readUInt32BE(16), head.readUInt32BE(20)] : null;
}

// Wide tables and code scroll inside their own focusable region instead of widening the page on a phone.
const scrollable = (html) => html
  .replace(/<table>/g, '<div class="table-scroll" tabindex="0" role="region" aria-label="Table">\n<table>')
  .replace(/<\/table>/g, "</table>\n</div>")
  .replace(/<pre>/g, '<pre tabindex="0">')
  .replace(/&lt;kbd&gt;(.*?)&lt;\/kbd&gt;/g, "<kbd>$1</kbd>"); // the Guide's one inline HTML element

function navTree(items, current) {
  return `<ul>${items.map((item) => {
    const here = item.route === current ? ' aria-current="page"' : "";
    const kids = item.children.length && (current === item.route || current.startsWith(`${item.route}/`)) ? navTree(item.children, current) : "";
    return `<li><a href="${docsHref(item.route)}"${here}>${escape(item.title)}</a>${kids}</li>`;
  }).join("")}</ul>`;
}

function docsNav(current) {
  const sections = tree.filter((t) => t.route !== "Home").map((section) =>
    `<h2><a href="${docsHref(section.route)}"${section.route === current ? ' aria-current="page"' : ""}>${escape(section.title)}</a></h2>${navTree(section.children, current)}`).join("");
  const home = `<ul><li><a href="/docs/"${current === "Home" ? ' aria-current="page"' : ""}>Docs home</a></li><li><a href="/install/">Install</a></li><li><a href="/download/">Download</a></li></ul>`;
  return `<nav class="docs-nav wide" aria-label="Docs">${home}${sections}</nav>
<nav class="docs-nav narrow" aria-label="Docs (contents)"><details><summary>Contents</summary><div class="tree">${home}${sections}</div></details></nav>`;
}

const pageIds = new Map();
for (const [route, page] of pages) {
  const source = readFileSync(page.file, "utf8");
  const rendered = micromark(source, { extensions: [gfm()], htmlExtensions: [gfmHtml()] });
  const { html, ids } = headingIds(rendered);
  pageIds.set(route, ids);
  const body = `<div class="wrap docs">
${docsNav(route)}
<article class="doc">
${scrollable(rewrite(html, route))}
<p class="doc-foot">This page is generated from <code>docs/wiki/${escape(relative(WIKI, page.file))}</code>; the same page is under <strong>Guide</strong> inside Rynhult.</p>
</article>
</div>`;
  const plain = decode(html.replace(/<h1[\s\S]*?<\/h1>/, "").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
  const description = plain.length > 155 ? `${plain.slice(0, 152).replace(/\s\S*$/, "")}…` : plain;
  const path = docsHref(route);
  write(join(OUT, path, "index.html"), layout({ path, title: page.title, description, body }));
  sitemap.push(path);
}

for (const [from, to, anchor] of anchorChecks) {
  if (!pageIds.get(to)?.has(decodeURIComponent(anchor))) problems.push(`${from}: missing anchor #${anchor} on ${to}`);
}

for (const name of attachments) {
  mkdirSync(join(OUT, "docs/media"), { recursive: true });
  copyFileSync(join(WIKI, ".attachments", name), join(OUT, "docs/media", name));
}

// --- Static files --------------------------------------------------------------------------------------------------
mkdirSync(join(OUT, "keys"), { recursive: true });
copyFileSync(join(ROOT, "keys/rynhult-release-key.asc"), join(OUT, "keys/rynhult-release-key.asc"));
write(join(OUT, cssName), css);
for (const name of (PRODUCT_ONLY ? ["og.png"] : ["favicon.svg", "favicon.ico", "icon-180.png", "icon-192.png", "icon-512.png", "og.png"])) {
  copyFileSync(join(SITE, "assets", name), join(OUT, name));
}
if (!PRODUCT_ONLY) {
  mkdirSync(join(OUT, "fonts"), { recursive: true });
  copyFileSync(join(SITE, "assets/fonts/noto-sans-latin-wght.woff2"), join(OUT, "fonts/noto-sans-latin-wght.woff2"));
}
write(join(OUT, 'release.json'), JSON.stringify({ ...release, phase: PHASE }, null, 2) + '\n');
for (const name of ["overview.png", "phone-overview.png"]) {
  mkdirSync(join(OUT, "media"), { recursive: true });
  copyFileSync(join(WIKI, ".attachments", name), join(OUT, "media", name));
}
write(join(OUT, "robots.txt"), `User-agent: *\nAllow: /\nSitemap: ${ORIGIN}/sitemap.xml\n`);
write(join(OUT, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${
  sitemap.map((p) => `<url><loc>${ORIGIN}${escape(p)}</loc></url>`).join("\n")}\n</urlset>\n`);

if (problems.length) {
  console.error(`site build: ${problems.length} problem(s)\n${problems.map((p) => `  - ${p}`).join("\n")}`);
  if (!process.argv.includes("--allow-problems")) process.exit(1);
}
console.log(`site build (${PHASE}): ${sitemap.length} pages, ${attachments.size} docs images → ${relative(process.cwd(), OUT) || "."}`);
