# Product website build

Run `node site/build.mjs` from the app repository after installing its locked web
dependencies in `web/`. The default output, `site/dist/`, remains a standalone
preview with the legacy landing content and both release phases available via
`--phase release` or `--phase coming-soon`. Release metadata comes from
`site/release.json`; the effective phase is also written to the output's
`release.json`.

For the public site, build only product-owned paths:

```sh
node site/build.mjs --product-only --out /tmp/rynhult-product
```

This includes the docs, Install, Download, Security, Licence, 404, public key,
media, `og.png`, robots, sitemap, `release.json` and stable `site.css`. CSS links
carry `?v=<content-hash>` for cache invalidation. The root stays in the sitemap
but `index.html`, favicons, icons and `fonts/` are supplied by the landing build.
Do not deploy the product-only directory by itself.

From the separate `rynhult-site` repository, combine into a different directory:

```sh
npm run combine -- --product /tmp/rynhult-product --out /tmp/rynhult-combined
```

Combining validates path ownership and renders the landing page using the product
release metadata. It does not deploy anything. Preview the combined folder with a
local static server, then run the app repository's browser review:

```sh
PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright \
  node site/test/review.mjs http://127.0.0.1:19631 /tmp/rynhult-review
```

`node --test site/test/site.test.mjs` tests both standalone phases and product-only
ownership. The browser review samples product pages in both themes at 320, 390
and 1440 pixels, checks shared chrome against the landing page, keyboard skip
navigation, sideways overflow, WCAG 2.2 AA axe rules and external resource requests.
The combined build permits the landing page's own local scripts; product pages
retain their stricter no-script CSP.

The landing tokens, base styles, logo partials and header/footer were copied from
`rynhult-site` commit `3bec36dc9832a836b00c326a996c8893fb52fcb1`.
The CSS copies carry that revision in comments. Update these copies together when
landing chrome changes; product builds never import from another checkout.
The coming-soon standalone preview keeps its pre-release wording and navigation
without linking to pages that phase intentionally does not build.
