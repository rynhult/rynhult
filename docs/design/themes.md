# Declarative community themes through Catalog

> **Status: approved design (Josh, 2026-11-01).** Not yet implemented; implementation follows the redesign phases and their own reviews and qualification.

Status: proposed P5 implementation contract, 2026-11-01. **Design only; theme import is not implemented by this document.** Baseline: `main-475e` (`475e6063`). P6 executable plugins remain separately approved and qualified work.

## Intent and evidence

P5 lets a person choose a familiar appearance without changing their work, permissions or agent configuration. Built-in Rynhult remains usable without any community package. Appearance (System / Light / Dark), theme package, workspace layout and authority are four distinct choices.

The supplied planning handoff's **COMMUNITY THEMES AND PLUGINS**, W9 and P5, and AC-042–AC-047 govern this proposal. W9 says Account → Appearance → Themes and future Installation → Extensions must reuse Catalog, preserving its ownership and visibility. The handoff requires declarative tokens/assets first, separate preview/apply, recoverable built-ins, no unrestricted CSS/scripts/network content, and preservation of provenance, pins, private overrides and compiled launch choices.

The assigned `workflow/theme-import.jpg` shows an Appearance selector, built-in card, community-import explanation and review card with publisher, contents, compatibility, account scope, Preview and Cancel. The publisher is explicitly **Not verified**, and the screen labels the workflow proposed and its package illustrative. Those are observations, not evidence that a transport, private import store, plugin runtime or validator exists. This design retains that hierarchy and adds explicit validation failures, separate Apply, keyboard/phone behavior and recovery. The private mockup is not copied into the repository.

Current implementation anchors:

- [Design system](DESIGN.md) and `web/src/index.css`: semantic colors/surfaces, body/focus contrast rules, fixed fonts, selection styling and elevation. Theme aliases such as `line` map to CSS variables such as `--border`; accepting arbitrary CSS variable names would exceed the contract.
- `web/src/lib/theme.ts` and `web/src/main.tsx`: `acc-theme` stores the browser's System/Light/Dark choice, applies `.dark`, follows OS changes and updates theme-color metadata. There is no community package or synchronized personal theme preference today.
- `web/src/components/Terminal.tsx`: xterm currently uses its own explicit palette. Changing `--term-bg` alone does **not** recolor terminal output. P5 must preserve or deliberately integrate this boundary.
- `src/acc/server/{catalog,catalog_store,catalog_import,catalog_restore}.py` and `api/catalog.py`: existing item types, owner-only import/mutation, pinned GitHub review, expiring in-memory previews, serialized writes, validation/undo, restored generations and local overlays. Existing previews are not yet a general actor-bound typed-package protocol.
- [Catalog Guide](../wiki/User-Guide/Catalog.md), [restore contract](../CATALOG-RESTORE.md) and [decision 0003](../decisions/0003-catalog-restore-layer.md): custom IDs win, profile overrides/exclusions and MCP vault references survive restores. No theme may enter provider skills/tools or change executable pins.

## Scope, authority and storage

Use one typed package-review service **inside Catalog**. Add `theme` as a presentation-only kind with a separate validator and immutable blobs; do not coerce it into a skill or MCP record. Keep legacy categories/routes and selected built-in restore flows functional. Do not expand `Item.providers`/launch compilation to include themes.

Initial scope preserves today's administration boundary: **owners import, update, restore and remove shared Catalog packages**. Authenticated people can list only themes they may already see, preview approved installed versions, and apply/reset their own preference. Members do not gain Catalog mutation, private-package access or URL-fetch authority. Account → Appearance → Themes exposes the personal chooser to members and Import theme only to owners; Catalog's theme category opens the same review/installed records. Installation → Extensions may explain future plugins but must not advertise a working installation action. A future private personal import is a distinct ownership decision, not silently introduced here.

No second import store: theme files and review metadata belong to Catalog's immutable content-addressed generations and existing locking/validation domain. A personal preference stores only `{package_id, digest}` plus revision; it is not another copy of package metadata or bytes. Themes cannot see or reference MCP vault material. Existing private/local overrides retain their visibility; a global content hash is not a read capability. Asset downloads require the same visibility check as the package.

Proposed persistence for implementation review (no migration number reserved): a typed Catalog package index/immutable blob adapter, plus personal presentation preferences keyed by installation/person. Optional workspace override key = installation/person/stable project-or-private-workspace identity, **not a path, slug, browser tab or session**. Overrides are personal even in a shared project. Person/object access is checked on every operation and again at commit. Never store this selection in the P2 layout payload.

## Manifest v1 and immutable identity

Local upload of a bounded package is the first supported transport. Later pinned GitHub import must reuse Catalog's constrained fetch path and immutable commit resolution; arbitrary URL download, redirects to private networks and package-managed fetches are not permitted. No public marketplace, discovery, billing, ratings or dependency installer in P5.

Package = ZIP containing `theme.json`, explicitly listed raster assets and optionally a declared plain UTF-8 `LICENSE.txt` (≤64 KiB). JSON is UTF-8 with duplicate keys and unknown fields rejected. Stable ID uses Catalog's existing lowercase letter/digit/dash grammar, maximum 63 characters, and is scoped with kind `theme`. Display name is not identity. A source/publisher change does not automatically authorize taking over an ID.

| Field | v1 contract |
|---|---|
| `schema_version` | Exact integer `1`; unknown versions refuse before staging a usable package |
| `id`, `type`, `name` | Stable ID, literal `theme`, plain name ≤80 characters |
| `author` | Plain display string ≤120 characters; unverified unless independently established |
| `source` | Declared source description and optional HTTPS provenance URL; never fetched/rendered as active content by the manifest |
| `version` | Strict semantic version; no version-as-floating-update channel |
| `compatibility` | Explicit `app` bounded semantic-version range and `theme_api: 1`; prereleases require explicit inclusion, never lexical comparison |
| `license` | Object with SPDX identifier (or `unknown`) and optional `file` descriptor for `LICENSE.txt` (byte length and SHA-256); unknown is visible and prevents redistribution claims |
| `digest` | `sha256:` full lowercase digest of the canonical payload described below; a claim to verify, not publisher authentication |
| `dependencies` | Must be `[]` in v1; no theme stacking, remote fonts or transitive retrieval |
| `tokens` | Required `light` and `dark` maps, resolved against the same versioned built-in defaults; only approved keys/types below |
| `assets` | Explicit inventory: relative path, media type, byte length and SHA-256; optional descriptive alt text |

Canonical payload hashing avoids a self-referential digest: remove only the root `digest` field, canonicalize the remaining JSON with a documented deterministic serializer (unique keys, sorted object keys, UTF-8, restricted finite integers; no floats), then hash a domain-separated, length-prefixed manifest plus all declared asset/licence files in sorted path order. Asset bytes, their names, compatibility, author and token contributions are covered. The validator recomputes every asset hash and the package digest; the browser cannot supply a trusted digest. Raw archive hash is recorded separately for diagnostics. The canonicalization has cross-language golden vectors before implementation ships. Repository Catalog revision is not a substitute for the full package digest.

Provenance records include uploading actor/time, transport, resolved repository/commit/path if applicable, declared versus verified publisher/source, licence, package digest, validator version, and validation report. HTML in metadata is escaped; bidi/control characters and oversized names/URLs are rejected. A declared URL, checksum or successful accessibility check is not a trusted-publisher badge.

## Validator and contribution boundary

Validation is server-authoritative, deterministic, resource-bounded and completed **before** preview or application. No “Add it anyway” bypass exists for a theme's hard safety/contrast errors; legacy skill risk acknowledgements keep their current semantics. Proposed initial limits: archive 2 MiB, expanded total 4 MiB, manifest 64 KiB, 16 files, asset 512 KiB, image dimensions ≤1024 each and aggregate decoded ≤4 million pixels. Refuse excessive compression ratios, encrypted/multi-volume archives, nested archives, duplicate/case-colliding paths, absolute/drive/UNC paths, `..`, backslashes, symlinks/hardlinks/special files and unlisted entries. Extract privately with bounded streaming; never write through an existing symlink. Reject trailing/ambiguous JSON content and parsing depth >16. Worker/decoder timeout or error produces a failed review, not partial acceptance.

The v1 allowlist is an explicit schema, not a `--*` prefix:

| Contribution | Allowed values / restrictions |
|---|---|
| Colors | Opaque six-digit sRGB hex for `bg`, `panel`, `panel-2`, `panel-3`, `border`, `border-strong`, `border-input`, `text`, `muted`, `faint`, `accent`, `accent-strong`, `accent-ink`, `status-ink`, `ok`, `wait`, `bad`, `info`, `idle`. Map through a generated, reviewed alias table into current tokens. No CSS expressions, alpha, `var()`, `url()`, selectors or user-defined variables. |
| Typography | `sans: system | inter`, `mono: system | jetbrains-mono`, referencing only app-bundled stacks. Font-size preset `default | comfortable` (comfortable scales text to 110%); no arbitrary font files, weights, ligatures or smaller text. Both presets must be tested. |
| Spacing | `density: default | comfortable`, resolving to app-owned spacing tables with a 44px touch minimum; no offsets, negative spacing, content hiding, line-height clipping or reduced target sizes. Compact community density is deferred. |
| Surfaces | `radius: default | soft`, `elevation: default | flat` map to fixed app presets, never arbitrary shadow/filter/opacity/layout CSS. Both must retain control/focus boundaries. |
| Assets | PNG only, decoded and re-encoded to strip metadata under limits, for non-interactive theme-picker thumbnails. Validate package bytes first; derived sanitized assets get their own digest. No SVG, animation, fonts, HTML, external images, wallpaper behind work/security UI, logos replacing identity or assets used in CSS. Broader asset slots need a schema revision. |

Omitted allowed tokens resolve to the validator's exact built-in baseline; record the **fully resolved** palette and baseline version in the review. New app tokens do not inherit arbitrary publisher values. A changed baseline triggers revalidation. Unknown keys are errors, not silently ignored fallbacks. This bounded subset supports color/typography/spacing/surface intent without promising arbitrary redesigns.

Terminal overrides are deliberately **not accepted in v1** (`term-bg` included): retain xterm's app-owned background, foreground, cursor, ANSI palette and selection. Validate its containing surfaces and selectable-text fallback in both appearances, with an explicit “Terminal palette stays built in” note. If terminal palette customization is added later, require a complete versioned palette including all 16 ANSI colors, cursor and selection, apply via xterm options in place, and qualify readability without reconnecting. CSS-only recoloring cannot claim that support.

Neither tokens nor assets may replace/hide approval actions, security/host/person identity, recovery, guard refusals or authorization indicators. Their labels, icons, layout and semantics stay app-owned. Status hues retain green/amber/red/blue/idle categories through bounded app-owned hue bands; text/icon labels remain mandatory. This reduces spoofing but does not make a publisher trustworthy.

### Readability gate

Extend the existing `DESIGN.md` and `web/scripts/theme-contrast.test.mjs` pair matrix rather than making a theme-specific weaker standard. For **both** fully resolved appearances, require:

- Unrounded WCAG sRGB contrast ≥4.5:1 for all text sizes, including muted/faint/status/link labels on every surface and their actual 10%/12% tints, and primary/danger/warning action ink across hover/pressed/disabled treatments where text remains informational.
- Essential inputs, cursor and focus indicators ≥3:1 against each adjacent background. Decorative dividers need not meet this; they cannot become essential control outlines. Preserve the fixed ≥2px focus ring and offset and no clipping/obscuring.
- Composite selection colors computed against each relevant surface, with selected text ≥4.5:1 and a visible selection boundary ≥3:1. If current translucent selection cannot meet this, generate an app-owned opaque selection pair from validated colors; do not weaken the gate. Cover editor, Chat/code, plain selectable terminal text and xterm's unchanged selection/cursor separately.
- Status, warning, error and approval identity remain distinguishable with words/icons, forced-colors support and no color-only meaning. Preserve real error/action hierarchy; a numerical contrast pass alone is insufficient.
- Representative Chat, Terminal/ANSI output, Files/editor/diff, Inbox/approval, Settings/dialog/menu and Status board fixtures at 1440/390, 200% and 400% zoom/reflow, keyboard, reduced motion, OS high contrast and long translated labels. Axe plus visual/keyboard review; screenshots are not runtime or screen-reader qualification.

Some currently hardcoded colors may not yet follow tokens; audit and either integrate them with the same matrix or keep them explicitly app-owned before enabling import. Built-in failures uncovered by the expanded matrix must be fixed rather than exempting community themes. No claim that this design has qualified existing selection composites.

## Review, preview, apply and reset

One proposed typed review envelope contains `review_id`, kind, stable ID/version/digest, actor/visibility scope, base Catalog revision, validator/baseline version, compatibility and accessibility report, declared contributions/assets, provenance, conflicts, expiry and allowed next actions. Reuse Catalog preview infrastructure but extend actor binding, kind dispatch and compare-and-swap semantics; the current in-memory token alone is not this contract.

State flow: upload → bounded staging → validated review **or refused with reasons** → isolated visual preview → explicitly add approved package to Catalog → personal Apply. Applying an already approved installed package needs only the review/preview and personal Apply steps. Catalog installation and personal application are distinct operations and permissions. Cancel never installs or applies; abandoned staging expires after 15 minutes with bounded per-actor count/bytes. Unsupported types/versions do not receive an actionable Apply button.

Review is one-time, actor-bound, scoped to the exact digest/base revision/validator version, and expires after 15 minutes. Recheck authority, bytes, compatibility and current revision at commit under Catalog's existing lock. Concurrent import/update/restore or stale review returns 409 and asks for a new preview, never last-writer-wins. Repeated completed requests use a scoped idempotency key to return the same receipt; they cannot replay another person's review. Anonymous/agent callers cannot import; members cannot install; all mutations use current CSRF/origin protection and audit.

**Preview without apply:** use a sandboxed (`sandbox=""`, no scripts, same-origin, forms, popups or downloads), script-free preview document with locally generated app-owned fixture markup and validated token declarations only. No live private content, links/forms, session sockets or same-origin access. CSP blocks scripts, navigation, network and non-packaged images; sanitized assets are supplied as bounded local blobs. The host review/Apply/Cancel/recovery chrome stays built-in and outside the theme scope. Show desktop and phone samples, light/dark switch, terminal unchanged note, expected contribution diff and accessibility failures with token pairs. Do not apply temporary styles to the real document root or save a preference. Escape/Cancel/Back closes preview, discards staging when appropriate and restores focus to its trigger; no global appearance or layout mutation occurs.

At 390px, review metadata becomes a single readable column with wrapped hashes/URLs and separate Preview/Apply/Cancel controls; no horizontal scrolling requirement except explicitly labelled code/details. Keyboard users can reach both sample appearance choices and controls. Announce validation/apply outcomes politely, focus a linked error summary on refusal, honor reduced motion, and never switch system appearance merely to preview another sample.

**Apply:** explicit button names package/version and scope, default “My account”; workspace override is never preselected from a repository or manifest. Write the current person's preference with a revision precondition and audit package/digest/scope/outcome, without recording private project names in global logs. A stale preference returns 409 with current choice, not a silent overwrite. Transaction failure leaves the previous preference. Browser resolution failure falls back to built-ins with a visible note; it does not mark the failed package successfully rendered. Preserve the prior successful pointer for rollback. No processes, grants, layout rows, project files, provider pins or session launch snapshots are written.

## Personal resolution and recovery

Effective order: authorized explicit workspace override → person's account theme → built-in Rynhult. Each custom pointer includes an exact digest, never `latest`; a workspace override must not affect another person, project or private workspace. Switching context re-resolves tokens without remounting terminals/editors, clearing drafts/selection, resizing saved layouts or taking input control. Typography may cause geometry reflow; keep the same runtime and reading anchor, with no writer/input side effects.

System/Light/Dark remains a separate choice. Preserve current `acc-theme` behavior initially; theme package selection can synchronize as a personal preference without silently converting a device's Light choice into System. If appearance choice later synchronizes, migrate it explicitly with a conflict rule. Server-backed theme preference is authoritative; browser cache is keyed by installation/person/schema and cleared on sign-out/account switch, never shared across people. Initial unauthenticated/login/recovery pages always use built-ins; only validated cached personal tokens can avoid a post-login flash. Never use unvalidated CSS from localStorage.

Optional workspace overrides ship only after stable workspace identity/access checks and preference CAS are available. Until then, the UI offers account scope only and says workspace override is unavailable. This does not block account themes and does not invent a competing workspace/layout identity.

Reset scope is explicit:

- **Use account theme here** deletes only the caller's workspace override.
- **Reset account theme** selects built-in Rynhult for the caller; warn if existing workspace overrides remain effective elsewhere.
- **Reset all my theme choices** explicitly clears that person's account pointer and workspace overrides. Appearance System/Light/Dark and saved layouts remain unchanged unless separately requested.

Provide a fixed built-in recovery entry in Appearance and a documented same-origin `?appearance=builtin` escape for the current view. Interpret it before custom tokens load, preserve it across recovery navigation, and offer authenticated reset from trusted chrome. It neither changes privileges nor silently writes preferences. Package stylesheet failure, missing assets, revoked visibility, uninstall, incompatibility or corruption all resolve to built-ins, explain why and preserve a disabled old pointer for deliberate recovery rather than crashing startup. Never remove the built-in palette.

## Updates, compatibility and rollback

No automatic activation. Show available update versus installed versus applied digest separately; review token/contribution/provenance/compatibility changes and require explicit install and Apply. Updating a shared Catalog entry does not repoint people's existing digest selections. Same ID/version with different bytes is a conflict/quarantine, not an update. Changed publisher/source requires explicit owner review; a digest is integrity, not signature-based publisher trust.

Revalidate on app/theme API/baseline/validator change. Unknown schema, unsupported dependency, missing variant or app range mismatch is blocked before mutation. A previously applied now-incompatible package falls back to built-ins with the old selection retained as unavailable; never reinterpret unknown tokens. Compatible prior versions can be explicitly restored by digest after current revalidation. Keep referenced and previous-successful blobs; garbage collection cannot remove an active/rollback reference. Removal requires owner authority, reports affected choices without exposing private identities, disables references and falls back safely; it cannot silently substitute another publisher's package.

Use Catalog's installed → restored → custom precedence and whole-catalog validation/undo. New built-ins never overwrite a custom ID; restore skips collisions or requires an explicit alternate unused ID. Themes do not reset profile exclusions/private overrides, change MCP vault references or touch running sessions' compiled tools. A bad theme must be quarantined without preventing loading the unrelated operational Catalog. Downgrade to an app without theme support ignores the new typed index/preferences safely and uses built-ins; implementation must test this separation instead of adding unknown theme kinds to a legacy parser that fails startup.

## Future P6 typed review, not execution permission

Reuse package identity, provenance/digest, compatibility, actor-bound review, contribution diffs, transaction receipts and rollback states. Dispatch `plugin` to a **different** validator and lifecycle. P5 refuses it, with an explanation that executable plugins are unavailable. Never interpret a theme's unknown field as a plugin hook.

P6 requires separately reviewed typed capabilities for data/actions, permitted installation/project/person scope, isolated origin/frame or process, mediated request authentication, rate/resource limits and revocation. Install disabled; enable separately after scoped consent. Owner installation is not access to every private project, transcript, credential or terminal. Contribution slots cannot replace approvals, recovery or identity controls. Deny arbitrary DOM/main-process access; frame sandbox alone is not a capability grant. Access-widening updates require renewed review. Disable/revoke prevents new actions, aborts cancellable in-flight requests and reports irreversible/uncertain work honestly. Missing/uninstalled contributions leave core views usable. Existing session tool compilation/pins remain frozen until an independently authorized transition. P6 runtime choice and permission schema are unresolved implementation reviews, not delivered by P5.

## Delivery and acceptance

| Slice | Deliverable / gate |
|---|---|
| P5.0 — contracts | Freeze manifest/canonical digest vectors, token/contrast matrix, role/visibility policy and bounds. Map remaining hardcoded surfaces. Keep import unavailable until gates below pass. |
| P5.1 — Catalog | Typed validator/staging/review/install adapter and immutable rollback; preserve legacy import/restore routes. Unit/property tests for parser/archive abuse, hashes, both palettes, scope, expired/wrong-actor/replayed/stale reviews, CAS, update races and atomic failures. |
| P5.2 — personal UI | Account chooser, isolated realistic preview, explicit Apply/Cancel/Reset/recovery, authenticated preference API/cache/sign-out handling. Add workspace override only when stable identity is qualified. No real-root preview or plugin buttons. |
| P5.3 — qualification | Focused Catalog regression/API/browser/accessibility/continuity and upgrade/downgrade tests; independent security review before enabling imports. Guide documents actual transport, scopes, limitations and recovery. Coordinator owns full gates. |

| Acceptance | Required proof |
|---|---|
| AC-042 shared home | Catalog and Appearance show the same ID/digest/state; owner/member/private visibility enforced, plugin unavailable, no alternate import/policy store. |
| AC-043 parity | Representative skill, subagent, MCP/profile preview/import/restore including flagged risk, pinned versions, custom collisions, private/profile overrides and vault references; compare catalog results and frozen running launch snapshots before/after theme operations. |
| AC-044 validation | Valid package plus duplicate/unknown fields, malicious paths/ZIP bombs, scripts/CSS/SVG/remote/font entries, hash tampering, unsupported dependencies and both-appearance contrast failures: no execution/network, installed bytes or partial preference change. |
| AC-045 scope | Owner/member/two-person/two-workspace tests, concurrent stale tabs, preview cancel/expiry/Back, Apply/Reset/rollback/offline failure; unchanged policy/process/project/layout snapshots and same terminal connection/input/IME/draft/selection through reflow. Hiding an action never weakens its API authorization. |
| AC-046 accessibility | Both appearances, 1440/390, zoom/reflow, keyboard/focus, reduced motion, forced colors and representative realistic surfaces above; contrast matrix and axe plus human screen-reader/terminal-output qualification. Host review/recovery chrome remains readable under every package. |
| AC-047 compatibility | Unknown type/schema/app version, prereleases, missing dependency/asset, changed publisher, private collision, stale revision, failed commit/render, app upgrade/downgrade and removal: explain outcome, no silent overwrite/widened permission, recover previous digest or built-ins. |

Risks requiring implementation review: token coverage and selection composites may expose current built-in failures; font/density changes can disturb terminal/editor geometry; asset decoders and archive intake are attack surfaces; Catalog type expansion must not affect executable pins; preference caching can leak private metadata across accounts; stale previews and restored/custom precedence can overwrite choices if not CAS-bound. The conservative bounds, built-in terminal palette, restricted asset slots, independent trusted recovery chrome and exact-digest references are proposed defaults. Broader assets, personal imports, synchronized appearance mode and executable isolation remain explicit later decisions, not reasons to weaken P5's gate.
