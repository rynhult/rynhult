# Rynhult design system (v3, 2026-10-24)

Rynhult is an **Operate** surface (impeccable's term): a dense, calm control room used mostly from a MacBook, sometimes from a phone.
References: Linear (navigation, density, ⌘K), Vercel and Railway (status and project cards), GitHub (lists), Grafana (meters).
Everything visual is defined in `web/src/index.css` (shared tokens), `web/src/shell-palette.css` (new-shell palette tokens) and `web/src/components/ui.tsx` (primitives). Pages compose those; they never define colours, radii or type sizes of their own.

## Principles
1. **Status first, prose second.** The first screen answers "what needs me, what is running, is acc-host healthy".
2. **Healthy is quiet.** Green appears only as small dots; colour words, badges and borders are for problems (amber = needs attention, red = broken).
3. **Never show "nothing here" for data that failed or hasn't loaded.** Use `QueryState`: skeleton while loading, an error note on failure, a stale note when old data is still shown, and only then the empty state.
4. **Show provenance.** Every number says where it came from and how old it is (hook, statusline, app-server, snapshot).
5. **One way to do each thing.** One page frame, one header, one session row, one meter, one confirm dialog.

## Tokens (`index.css`)

The Rynhult kit supplies Charcoal, Slate, Frost, Snow, Periwinkle, Text and Muted. Dark surfaces step from exact Charcoal to exact Slate; Text and Muted keep the supplied values, and Frost identifies idle sessions. The light theme uses exact Snow and Charcoal, white cards, and blue-grey Frost tints. Slate-derived text replaces the kit’s light ink on these surfaces. Periwinkle is retained as `accent-strong` in dark mode; `accent` is a lighter tint so small links on tinted badges remain AA. Light accents are darker Periwinkle shades. Status hues remain green, amber, red and blue, with brighter dark values and deeper light values to preserve or improve stage-1 contrast on every corresponding surface.

| Token | Dark | Light | Use |
|---|---|---|---|
| `bg` | #10191f | #f0f3f3 | app background |
| `panel` | #15232b | #ffffff | cards, menus and dialogs |
| `panel-2` | #1c2e38 | #e8eff3 | secondary surface |
| `panel-3` | #263b46 | #dae5ed | hover and active surface |
| `line` | #38515f | #c0cfd8 | decorative dividers and card outlines |
| `line-strong` | #526d7c | #9cafbc | decorative emphasis |
| `line-input` | #8da4b2 | #627987 | essential field boundaries (≥3:1) |
| `fg` | #f2f5f4 | #10191f | primary text |
| `muted` | #a4b4be | #485d68 | secondary text |
| `faint` | #93a7b4 | #526671 | tertiary text |
| `accent` | #abb6ff | #414fa8 | links, focus ring, actions and meters |
| `accent-strong` | #939ef2 | #3d4c9e | Periwinkle emphasis |
| `accent-ink` | #10191f | #ffffff | text on solid accent actions |
| `status-ink` | #10191f | #ffffff | text on solid danger / warning count badges |
| `ok` | #84ecb7 | #086039 | healthy |
| `wait` | #ffd78d | #794200 | warning / needs attention |
| `bad` | #ffa0a0 | #aa1e24 | danger / failure |
| `info` | #b6ddff | #174f9e | information |
| `idle` | #b9cfe1 | #50657a | idle / stopped |

`line` and `line-strong` are decorative. Essential control boundaries use `line-input`, and keyboard focus uses `accent` with a 2 px outline and 2 px offset. Status always has words or a labelled icon as well as colour. Danger, warning and healthy colours remain distinct from the brand accent. Native controls use the matching `color-scheme`. The browser theme-colour metadata uses Charcoal / Snow; the installed-app splash uses Charcoal.

### Selection readability (P5)

App text selection uses opaque `accent` / `accent-ink`, preserving the text and boundary contrast above in both appearances. The terminal retains its app-owned palette: xterm selection `#52627a` against `#0b0d10` has 3.14:1 boundary contrast and `#e6e8eb` selected text has 5.05:1 contrast. The plain selectable terminal fallback uses opaque `#abb6ff` / `#10191f` in both appearances. Community palettes cannot override terminal colors; generated custom selection pairs and trusted built-in review chrome are checked separately.

### Computed contrast (WCAG 2.x, 2026-10-24)

Ratios use sRGB channel linearization (threshold 0.04045), relative luminance weights 0.2126 / 0.7152 / 0.0722 and `(Llighter + 0.05) / (Ldarker + 0.05)`. Values below are rounded to two decimals; the automated gate uses unrounded values. All body text requires 4.5:1, including small labels; essential boundaries and focus require 3:1. Large text also meets the stricter body threshold.

**Dark: text against each surface**

| Foreground | bg | panel | panel-2 | panel-3 |
|---|---:|---:|---:|---:|
| `fg` | 16.21 | 14.64 | 12.79 | 10.66 |
| `muted` | 8.34 | 7.54 | 6.58 | 5.48 |
| `faint` | 7.14 | 6.45 | 5.63 | 4.69 |
| `accent` | 9.20 | 8.31 | 7.26 | 6.05 |
| `accent-strong` | 7.12 | 6.44 | 5.62 | 4.68 |
| `ok` | 12.42 | 11.22 | 9.79 | 8.16 |
| `wait` | 13.00 | 11.75 | 10.26 | 8.55 |
| `bad` | 9.14 | 8.25 | 7.21 | 6.01 |
| `info` | 12.51 | 11.30 | 9.87 | 8.22 |
| `idle` | 11.07 | 10.00 | 8.73 | 7.28 |
| `line-input` | 6.85 | 6.19 | 5.40 | 4.50 |

**Light: text against each surface**

| Foreground | bg | panel | panel-2 | panel-3 |
|---|---:|---:|---:|---:|
| `fg` | 15.94 | 17.78 | 15.30 | 13.90 |
| `muted` | 6.19 | 6.91 | 5.94 | 5.40 |
| `faint` | 5.38 | 6.00 | 5.17 | 4.69 |
| `accent` | 6.47 | 7.22 | 6.21 | 5.64 |
| `accent-strong` | 6.90 | 7.70 | 6.63 | 6.02 |
| `ok` | 6.86 | 7.66 | 6.59 | 5.98 |
| `wait` | 7.26 | 8.10 | 6.97 | 6.33 |
| `bad` | 6.44 | 7.19 | 6.19 | 5.62 |
| `info` | 7.11 | 7.93 | 6.82 | 6.20 |
| `idle` | 5.40 | 6.03 | 5.19 | 4.71 |
| `line-input` | 4.09 | 4.56 | 3.93 | 3.56 |

**Composite backgrounds and action ink**

Tinted rows report the minimum over all four surfaces. Status baselines are the corresponding minimum on stage 1; the gate compares each surface individually, with both 10% and 12% tints.

| Pair | Dark | Light | Stage-1 dark | Stage-1 light |
|---|---:|---:|---:|---:|
| `accent` on own 12% tint | 4.76 | 4.78 | 5.33 | 4.02 |
| `ok` on own 12% tint | 6.08 | 5.02 | 5.97 | 3.81 |
| `wait` on own 12% tint | 6.37 | 5.29 | 6.23 | 4.20 |
| `bad` on own 12% tint | 4.81 | 4.63 | 4.62 | 3.91 |
| `info` on own 12% tint | 6.10 | 5.19 | 6.01 | 4.11 |

| Solid action / count ink | Dark | Light |
|---|---:|---:|
| Primary action | 9.20 | 7.22 |
| Primary action hover (110%) | 10.64 | 6.31 |
| Danger count | 9.14 | 7.19 |
| Warning count | 13.00 | 8.10 |

Solid status counts use `status-ink` (Charcoal in dark mode, white in light mode); the previous fixed black ink became unreadable on the deeper light-theme fills. The browser regression gate measures the rendered Inbox count directly, because automated axe sampling may omit a one-character label.

The contrast regression test (`node --test web/scripts/theme-contrast.test.mjs`) also checks 10% notes and 12% badges over all four surfaces against the stage-1 ratios, for every status. Idle uses a solid dot and a muted label (there is no idle-tinted badge); its hypothetical tints receive a non-regression stress check as well. It checks small text on tinted accent badges, input boundaries, focus, solid status-count ink, and primary-button ink at rest and at the 110% brightness hover state. Axe covers the rendered combinations, including translucent and nested backgrounds.

Elevation remains surface steps plus `shadow-card` (cards) and `shadow-pop` (dialogs, palette). Layout, density, spacing and type scale stay unchanged. The kit’s Noto Sans wordmark is already outlined in its assets; the UI keeps its self-hosted fonts and adds no font download. Login uses “One place to supervise your coding agents.” as its subtitle; authentication screens use “Your agents. Your machines. Your call.” in the existing footer slot.

**Type:** Inter Variable (self-hosted, `cv11 ss01 ss03`) and JetBrains Mono Variable. Scale: 11 (labels, uppercase section labels), 12–12.5 (meta), 13–13.5 (body), 15 (dialog titles), 20 (page titles), 22 (stat values). Numbers that update use `.tnum`.
**Spacing:** 4px grid; cards use `px-4 py-3`; pages use `space-y-6`. **Radius:** 6px controls, 10px cards, 12px dialogs.

## Primitives (`components/ui.tsx`)
- `Page` (one width: 1280px, or 1600px with `wide`) and `PageHeader` (title, description, meta line, actions; it also sets the tab title).
- `Card` and `CardHeader` (icon, title, wrapping subtitle, actions below the heading on narrow screens), and `SectionLabel`.
- `Stat` (label, icon, value, detail, optional meter; tone only when something is wrong) and `Meter` (accent, amber above 60%, red above 85%; always labelled).
- `Badge`, `Dot` (status dot; pulses only for live work and respects reduced motion).
- `Tabs` (ARIA tabs: one Tab stop, ←/→/Home/End move and select; with an `id` the tabs control the `TabPanel` of the same id; scrolls the selected tab into view, fades an edge with more tabs beyond it, and `phone` names the tabs a phone shows, the rest under "More"), `Segmented` (radio group: one Tab stop; arrows wrap and select, Home/End select the ends), `ToggleChipGroup` (fieldset and native pressed buttons for independent choices), `Toolbar`, `SearchInput`, `Kbd`.
- `Popover`: a button that opens a small panel (menus, a phone's Details sheet); Esc and an outside click close it and focus returns to the button.
- `Button` (primary / secondary / ghost / danger) and `IconButton` (always has a label and tooltip).
- `Dialog`: focus trap, Esc to close, focus returned to the opener, a pinned `footer` for actions; form dialogs pass `dismissible={false}` so a stray click never discards input. Dialogs can stack (New project over New session): only the top one answers Esc and keeps Tab inside it, and a field marked `autoFocus` keeps the first focus. `sheet` is the full-height left panel (the phone navigation drawer).
- Links inside a sentence are underlined (`index.css`), never colour alone. Scroll boxes that hold text are focusable regions with a name.
- `useConfirm()` (`components/confirm.tsx`): the only confirmation pattern. It names the consequence, and `danger` is set for destructive actions. Never `window.confirm`.
- `QueryState` (passes Retry and an optional `errorTitle` to `ErrorNote`), `ErrorNote` (a 4xx is ACC's own sentence, shown as is; a 5xx or a network failure gets a plain sentence with the raw text under **Details**; a 404 page is a not-found state with a way back, not an error), `StaleNote`, `Empty` (with icon), `Skeleton`, `LoadingRows`, `Meta`.
- `announce(text)` and `Announcer` (at the app root): the app's **one** polite live region. A page announces a state change once ("api-refactor: Needs you", "Terminal is read-only"); badges and labels are never live regions of their own.
- `Note`: an amber (needs attention) or red (broken) note about one item, such as a warning under a request. Only for the exception.
- Shared components:
  - `SessionList`: the session row layout everywhere (name/project, status, agent, activity, actions).
  - `StateBadge` and `sessionState()`: the one status map.
  - `PublishSummary` and `PublishActions` (`components/PublishRequest.tsx`, rules in `lib/publish.ts`): one summary of a publish request in Inbox, Overview and Publishing. Facts on one line, warnings as `Note`s; Approve asks through `useConfirm` when there is a warning.
  - `lib/words.ts`: words for engine identifiers (see **Words**); the raw value goes in a tooltip.
  - `lib/order.ts`: the one order. Session lists group Needs you → Working → Idle → Stopped, newest first; the Inbox groups Waiting sessions → Approvals → Alerts → Notes.
- `NewProjectDialog` (`components/NewProject.tsx`): the one way to make a project folder (empty, a clone, or a new GitHub repository), always under the person's own workspaces folder, with trust decided in the same step (on for an empty folder, off for a clone). Used by Projects, the Overview's **Get started** list and New session.
- `NewSessionDialog`: What (where, project, task, name) → Who (agent, profile, keys, where it runs) → **Advanced**, closed, whose one-line summary (`advancedSummary` in `newSession.ts`) names every choice inside it, including the one **Run** choice (Once · Several agents · With helpers · On a schedule). The draft survives closing; **Discard draft** asks first.
- **Words for counts** (owner decision 0008, Q1): **Needs you** counts sessions blocked on a person (sidebar group, Overview stat, Sessions filter). The **Inbox** badge counts every open Inbox item.
  - `ProviderBadge`: neutral, with `ProviderMark`, a small square in the brand colour (round dots mean status).

## Words
People read words, not engine identifiers. The exact value stays reachable in a tooltip or a disclosure. Maps live in `lib/words.ts` (`sourceWords`, `eventWords`, `auditWords`, `settingWords`) and are unit tested in `e2e/01-units`.

| Engine says | ACC shows |
|---|---|
| `acceptEdits` / `bypassPermissions` / `default` | Accept edits / Runs without asking / Asks before acting |
| `workspace-write` · `on-request` (Codex) | Can edit the project · Asks when it needs to |
| `claude_hook`, `supervisor`, `tmux` | Claude Code, ACC, Process state |
| `SessionStart`, `Stop`, `PreToolUse`, `run_launch` | Session started, Turn finished, Using a tool: Bash, Started |
| `idle → waiting_input` | Now Needs you (was Idle) |
| `terminal.attach {"mode":"write"}` | alex opened api-refactor's terminal to type (folded per person, session and day) |
| verified / heuristic | (nothing) / guessed |
| Launch spec | Setup |
| Agents (Catalog) / Deferred | Subagents / Not loaded by default |
| catalog revision `5dd47b…` | a tooltip |
| `hasTrustDialogAccepted` | "Sessions start without asking whether you trust this folder", with the exact keys under a disclosure |

## Shell
- **Sidebar:**
  - Groups: (Overview, Inbox, Sessions, Projects), then **Operate** (Publishing, Host, Providers), then **Configure** (Catalog, Audit).
  - Live sessions are pinned under the first group for one-click terminal switching, in the groups Needs you, Working and Idle: five each, then "+N more" (each group name links to that group on Sessions). The pinned list scrolls on its own so Operate and Configure stay in view.
  - The broker health dot sits next to the host name.
- **Top bar:** breadcrumb, a Live / Offline / Broker-unreachable indicator, **Jump to…** with a navigation arrow and ⌘K, and New session. **Jump to…** opens pages, projects, sessions and commands; its persistent **Search conversation content** link and query-specific **Search conversations for “…”** action open **Search**. **Search** keeps its magnifying glass, a one-line conversation-content description and a hint back to **Jump to…** in the top bar. Navigation and full-text content search are named separately; shortcuts are unchanged.
- **Attention:** one compact, normal-flow status line below the top bar, never an overlay or a stack of Inbox rows before Overview content. On a phone's session page (below `sm`), it sits below the content and above the bottom navigation, keeping the terminal near the top and operational warnings on screen. Inbox counts every open item; per-item actions live in Inbox. **Collapse other notices** hides only the line's ordinary-notice summary for this visit, not the items. Always-on offline and failed/stale backup counts remain until resolution or a timed snooze, even after acknowledgement. Foreground browser/PWA windows suppress system push banners; background notifications never require interaction.
- **Keyboard:** ⌘K / Ctrl-K opens the palette (pages, sessions, projects, actions). `C` starts a new session. A skip link leads to the content. In a session's terminal, Tab belongs to the agent; **F6** (Shift+F6 backwards) leaves both the input and accessibility rows, with a visible hint in the top bar. **Skip terminal** appears on keyboard focus before the grid and leads to the output controls. **Screen reader output** enables xterm's accessibility tree by default and persists per browser (not for Grid tiles). **Open as text** holds a selectable snapshot until Refresh; **Copy recent output** copies it. Capture reads bounded tmux scrollback; an unavailable capture explicitly labels the limited browser-buffer fallback.
- **Offline:** while ACC isn't answering (`useOffline`), buttons that change something are disabled with the reason in their tooltip, and cards keep their data with a `StaleNote`.
- **Touch:** `IconButton`, `Popover` triggers and row icon links are 40px on `pointer: coarse`.
- **Phone:** the Guide shows the article first, its page tree under **Contents** (a sheet); Grid shows one terminal with a tab switcher. A bottom tab bar (Overview, Inbox, Sessions, Projects), and the full sidebar in a drawer from the menu button. A healthy Live indicator is hidden for space; Offline or Broker unreachable shows as a red banner under the top bar. The session page has a one-line header with Details, Interrupt and Stop behind ⋯ on touch, and a key bar with ⏎, Esc and ^C first. Opening a terminal does not focus its text input on touch; its screen reader and text controls wrap within the phone width.
- External sessions (root-admin) open in Claude Remote Control from the sidebar and ⌘K; ACC never holds a terminal into a root session.
- Accounts merged into **Providers** (logins, re-login and plan usage in one place). `/accounts` redirects there.

## New shell (P1 and P2a, Preview)

### Mockup palette (desktop first)

`Shell` sets `html[data-shell-palette="mockup"]` before paint and removes it on
unmount. Cold Status board documents do not mount the shell; warm trips retain
its working-view resources while hiding frame chrome and removing the attribute. The attribute
scopes the overrides in `shell-palette.css`, including body-mounted dialogs.
The stylesheet loads with the lazy Shell chunk, keeping its tokens out of the
initial sign-in and standard-layout CSS payload. The standard
layout, sign-in and views outside the frame keep the original v3 palette.
`Shell.css` uses the sidebar surface and selected tint rather than approximating
those with the card surface. The main content uses `--panel`, matching the
mockup's `.ry-main`; the dark sidebar is darker than the content. Cards retain
their existing borders on that same panel surface. No preference, latch, layout
or terminal behavior changes.

Values come from the rendered `#rynhult-concept` / `#rynhult-recovery` styles in
the assigned workspace and exception-states prototypes, which override their
generic blue framework defaults. These are the warm neutrals/lavender visible in
the dark/light workspace mockups. Existing semantic names remain the public API:

| Mockup role / inner token | Rynhult token | Dark | Light |
| --- | --- | --- | --- |
| Background / `ry-bg`, `bg` | `--bg` | `#141719` | `#f6f5f2` |
| Main content and card / `ry-panel`, `panel` | `--panel` | `#1c2023` | `#fffefa` |
| Secondary/sidebar / `ry-side`, `side` | `--panel-2` | `#191c1f` | `#eeede8` |
| Hover surface (derived) | `--panel-3` | `#292e32` | `#e3e3df` |
| Foreground / `ry-ink`, `ink` | `--text` | `#eceeea` | `#242930` |
| Muted foreground / `ry-muted`, `muted` | `--muted`, `--faint` | `#a2abae` | `#5b6469` |
| Primary/ring (light contrast variant) / `ry-accent`, `accent` | `--accent` | `#b6b7fa` | `#414da6` |
| Selected/accent surface / `ry-tint`, `tint` | `--accent-tint` (new) | `#303044` | `#e9e9fa` |
| Divider / `ry-line`, `line` | `--border` | `#33393b` | `#dedfd9` |
| Control edge (production AA variant) | `--border-input`, `--border-strong` | `#8da4b2` | `#627987` |
| Primary hover (production variant) | `--accent-strong` | `#939ef2` | `#3d4c9e` |
| On primary (production variant) | `--accent-ink` | `#10191f` | `#ffffff` |
| Good (production safety variant) | `--ok` | `#84ecb7` | `#086039` |
| Warning (production safety variant) | `--wait` | `#ffd78d` | `#794200` |
| Danger (production safety variant) | `--bad` | `#ffa0a0` | `#aa1e24` |
| Information (production variant) | `--info` | `#b6ddff` | `#174f9e` |

The light mockup muted text `#626b70` is darkened slightly for AA over hover
surfaces. The light accent replaces mockup `#5355ad` with `#414da6` so accent
text and focus rings retain at least the legacy contrast on all four surfaces
(6.79 / 7.34 / 6.31 / 5.75:1 on background / panel / sidebar / hover).
Quiet dividers are decorative; controls use the stronger input edge.
Production warning/danger colours, status ink, badge opacity and focus-ring
thickness are retained so Security/Unrestricted and offline/attention states
remain prominent. Checks cover 4.5:1 text across the four surfaces, tinted status
badges, selected text and primary buttons, plus 3:1 control edges/focus colour.
Existing Inter/mono type, 11/12/15px shell label/meta/title scale, body type and
6px shell control radius remain unchanged; this slice transfers colour, not the
mockup's future multi-pane layout or renderer/terminal palette.


The new layout is the sole application frame after rc.4. There is no layout
preference, frame latch or `new_shell` feature flag. Authentication supplies the
stable opaque person ID used to namespace browser-local arrangements; its value
is unchanged from the retired preference endpoint. A cold `/board` document
loads without a frame; subsequent Board visits retain the mounted working views.
Migration 0088 removes stored layout preferences and the retired admin choice.

- **Navigation:** `shell/navigation.ts` derives rail and Tools from `navFor`'s role,
  group-administration, desktop and feature filtering. Its level-visibility hook
  defaults to showing all authorized entries. Rail items are buttons with
  `aria-pressed`; they open each primary destination and select its sub-navigation.
  Hosts opens `/hosts`, Tools opens `/catalog`; Overview and Grid hide the context column.
  `/board` hides new-shell chrome while retaining its resource owner; the desktop `/` → `/projects` redirect stays.
- **Project sidebar at volume:** Available projects is paged at 20, stably ordered
  by slug then ID. Search covers the full authorized list (name, slug, repository)
  and resets paging. Data-derived filtering/order is memoized against project data;
  keyed rows retain DOM identity across unrelated polling. Pinned/recent selections
  remain separate. Phone Browse gives the sidebar a bounded flex scroll area below
  its fixed mode controls. Rows use ordinary layout, without offscreen containment.
- **Project menu (polish 1):** project rows are native Overview links. The route
  determines the authorized menu: Work, Project and Working locations. Session
  routes resolve their project through the authorized session summaries and project
  list, including Files and Changes tabs. No previous selection is retained as a
  fallback for an unknown session. Changes
  is a new-shell-only index linking to existing session Changes views; it never
  attaches a terminal. Pinning is separate; Back to all projects opens the list.
- **Security shield (polish 1):** the owner's required header control is a native
  link to the existing scoped Security editor, styled as a compact shield button.
  Hover/focus exposes a dismissible, hoverable tooltip containing full current
  copy and scope. Neutral check, warning exclamation, unavailable question mark
  and offline slash distinguish states without relying on colour alone. Unrestricted
  uses solid warning fill and status ink; older-session counts use an amber marker.
  Stale data is labelled last known. Shared policy selection and member boundaries
  stay unchanged; frozen-session badges keep their dialogs.
- **Sidebar sizing (polish 1):** a focusable vertical separator resizes 208–400 px,
  using pointer capture, Left/Right steps of 8 px, Home/End limits and double-click
  reset to 256 px. Width and collapse persist in origin/person local keys. F6
  skips collapsed or unused context columns; selecting a desktop destination with sub-navigation expands it.
- **Header:** brand/host/broker, optional modules, project-aware New session and
  account. The optional defaults are Machine load, Plan usage, Go to (existing
  palette), Search conversations and Find project files. Security badge, Live
  indicator, connection banner and Update available retain Layout's
  visibility conditions and cannot be hidden or reordered by customization. The
  desktop header uses one compact row; connection and Inbox notices remain below.
  Below 1200 px optional modules move into Header tools. Desktop search/customize
  controls use icons with accessible names and hover titles. Provider summaries
  show the highest used window per provider; their keyboard-accessible popover
  lists every window and provenance.
  Only owners mount `/api/board`, `/api/providers` and `/api/accounts` readings;
  absent member-visible load data means no load module. Usage retains account,
  provider, window, age and source; no cross-provider utilization total.
- **Header customization (AC-008):** GET/PUT `/api/preferences/ui-header` stores
  `ui_header.<authenticated username>`, using the authenticated caller and CSRF contract.
  Installation tokens have their own principal namespace. The preference is
  `{order, hidden}`: order is a permutation of the five known optional module IDs
  (`machine_load`, `plan_usage`, `go_to`, `conversation_search`, `file_search`),
  hidden is a unique subset. Both lists are bounded to five; unknown/required IDs,
  duplicates, missing order entries and extra fields are rejected. Invalid stored
  presentation falls back to defaults; the identity inventory includes `ui_header.`.
  The client query is person-keyed. Explicit Save applies only a successful server
  response; errors retain the editor's choices. Reset fills the default draft for
  Save. This is a small last-save-wins preference, not a shared layout revision.
  Move up/down buttons support keyboard reordering and announce the new position.
  On phones, visible optional modules and customization live in Header tools, a
  mixed-content overflow popover with ordinary Tab navigation and Escape. The
  file-search controller stays mounted outside the popover so its target and
  shortcut survive closing/resizing. Owner reading queries retain their existing
  role gate and polling whether shown or hidden; members never mount them.
- **Keyboard/phone:** arrows move rail focus, Enter selects, F6 cycles header →
  rail → sidebar → content (reverse with Shift, skipping unused context columns). Existing terminal F6 escape
  takes precedence. Phone: one view, bottom rail and More sheet, unchanged
  terminal key bar. The sidebar edge separator supports keyboard adjustment. Shell styles
  use shared light/dark color tokens and centralized `--shell-*` type/radius tokens.
- **Local presentation:** origin is the installation namespace. The preference
  API returns an opaque UUID5 from the immutable, permanently reserved username,
  rather than the reusable SQLite User.id. Keys are
  `rynhult:shell:v1:<encoded origin>:<opaque person id>:<mode|width|collapsed|pins|recent>`.
  Service principals return no person ID and do not persist presentation. Pins
  and recents contain only project IDs, resolved against the current authorized
  list on every render. Other people's keys remain inert after sign-out. Storage
  errors fall back to current in-memory presentation; no draft is stored here.
- **Continue:** a normal link to `/sessions/:id`, including offline machines.
  It never launches, resumes, stops, sends input or requests takeover. **Slice
  exception to P2's view-restoration contract:** opening a terminal retains the
  P1 automatic input claim only when no writer exists (superseded for P2 working views below). An existing writer
  keeps control and the new window is read-only. No `take=1`, tty arbitration or
  terminal-lifetime changes. Resume/Stop/New session stay separate. Machine offline
  disables only Resume, with the machine reason. Stop, Interrupt and Archive keep
  their standard-layout state/connection conditions (S1); an offline machine alone
  cannot disable them.
  Continue lists use 20-row pages. Members read older-settings facts only through
  these visible sessions’ existing detail endpoints (at most 20 polls per 10s);
  owners reuse the existing policy inventory. No security API is widened. Older settings
  is informational; Resume eligibility and refusal wording belong to the server.
- **States:** reuse `session-bits` and `order`, without inventing a state layer:
  `starting` → Starting, `active` → Working, `waiting_input` → Needs you,
  `idle` → Idle, `exited` → Exited, `recovering` → Recovering,
  `recovery_needed` → Recovery needed, `stopped` → Stopped, `archived` → Archived.
  Stale observations are Unknown. Machine offline, recovery needed and an
  unconfirmed stop remain distinct; offline rows retain the last observed state.

- **Project filename search (AC-060):** shell-only header dialog, Ctrl/⌘ Shift F.
  It initializes from the working project's route on first opening, never the
  sidebar's browsed project. The selected project ID stays in memory for the frame's
  lifetime unless explicitly changed, resolved through the authorized project list.
  Project, machine, account and primary folder are visible; session worktrees are
  explicitly excluded. The project Files search API owns placement and permissions.
  Requests debounce 300ms, request at most 20 results, and cancel on typing, target
  change or close; no automatic retries, polling or retained search-result cache.
  Partial, offline/unavailable, error and complete empty states remain distinct.
  Shared announcements report progress. Native result links support arrows/Enter
  and open encoded Files deep links through existing unsaved-edit guards.

### Recent results (AC-007, new shell only)

The project Overview reuses its authorized Knowledge index and DocViewer for
handoffs, council reports and research findings. Sort by file modification time,
newest five indexed notes; call the age Updated, never completed. This is the
checkout's bounded index (up to 100 filenames/category), not private-worktree or
cross-source result history. View all opens the existing Knowledge section.
Session attribution requires a whole filename ID matching exactly one authorized
session in this project, including archived sessions. Missing/ambiguous references
stay Session not recorded; an unavailable session list stays unavailable. No new
backend APIs, grants or per-session detail fan-out. Initial failure, empty success
and stale retained data remain distinct. Use shared cards, tokens and wrapping at
phone widths. The standard Overview mounts no Recent results block.

### Hosts destination (AC-011, new shell only)

`/hosts` is the overview reached directly from the Hosts rail button. Machine
links in the contextual sidebar open their existing `/host` detail destination. `/host` keeps
its existing view and operations; the standard shell redirects the new alias
there. Host cards use shared tokens and one column at phone widths.

Owners reuse `/api/board` for machine inventory/node summaries and the existing
`/api/host` for the main machine's separate live and collector provenance. Members
only query their existing filtered `/api/host` main view, never board or node
inventory. Authorized session/project
queries supply visible active work and recorded working paths. Group placements
come through existing group membership/viewer checks and link to the group's
Projects surface, not personal-project routes. No endpoint or permission is
added. Unknown/failed/offline connection states never imply Idle or zero health;
observation and last-seen ages stay visible, including after a failed refresh.
Main CPU/memory use live `measured_at` (or explicitly labelled response receipt
when absent); disk uses collector `collected_at`, never board's mixed local-light
timestamp. Collector stale alerts remain visible beside fresh live readings.
Node readings name their summary source/age. Failed session refresh marks work
unavailable and dates retained saved counts using the session query's own last
success, in both sidebar and cards; machine freshness cannot date work counts.
### Authority changes in shared project/session lists

Project and session list query keys include current person, role and agent account.
Role changes do not borrow wider cached rows as placeholders during a fresh read
or failed refresh. Existing resource-prefix invalidation still reaches the scoped
queries. Project Overview selects from the same authority-scoped project list;
revoked project children cannot keep rendering cached Continue or result views.
Settings picker and shell sidebar browsing selection reset with authority; stored
pin/recent IDs remain inert unless resolved by the current authorized list. This
is a client visibility boundary, not a replacement for backend authorization or a
complete audit of every detail endpoint/cache.

### Settings scopes (AC-013, new shell only)

Account / Project / Installation organizes navigation to the existing sections.
Installation stays owner-only. Project choice resolves only against the existing
authorized project list; a context URL supersedes an earlier browsing selection.
Project links retain runtime/desktop/managed/private-workspace availability.
Members retain read-only project Security and no owner policy inventory.

Security shortcuts use the existing scoped URL and sole Security component;
Allowed folders shortcuts use the existing sole account root-list route, with
machine/account coordinates where known. Project policy does not become a
project-only filesystem grant. No editor, save API, permission or store is copied.
All existing section fragments, aliases and dedicated routes keep working; the
standard shell is unchanged. The new Project settings fragment is links only.
Hidden section mounting, draft caches, dialogs, validation/error reveals and
attention summaries retain their existing behavior. Scope navigation uses native
links and shared tokens, including phone wrapping and focus outlines.

P2 adds server-backed personal working layouts and retained terminal/editor
resources, as described below.

### Page chrome (U1)

`components/shell/PagePrimitives.tsx` supplies page-body presentation for the
application frame. Shared domain widgets retain their existing controls and
semantics; there is no classic delegation or layout context.

| Primitive | Contract |
| --- | --- |
| `ShellPage` | Existing 1280/1600px max widths; 20px desktop gutter/rhythm, 16px narrow gutter; 14px/1.5 base type. |
| `Workbar` | Optional `breadcrumb` React content (omitted unless a caller supplies actual context), 20px/600 h1, 13px description, wrapping actions/meta and a bottom divider. Retains document-title behavior. Use links for navigable breadcrumb parts. |
| `Surface`, `SurfaceHeader` | Semantic section/h2, flat panel with divided edges, no shadow/rounded container nesting; wrapping existing header/actions. Domain widgets retain their own styling. |
| `PaneTabs` | Existing `Tabs` API, IDs, panels, roving keyboard selection and overflow; 32px desktop pills, 44px coarse-pointer targets, semantic selected tint. |
| `PageStack` | Shared 20px vertical rhythm; legacy 24px rhythm outside the new shell. |

All colours are existing semantic tokens, including `accent-tint`; no new
palette or font family. `PagePrimitives.css` targets only explicitly rendered
primitive classes, not arbitrary cards, dialogs, warnings, terminal output or
recovery notices. Consumers can alias these exports to their existing local
Page/PageHeader/Card names; do not switch the shared legacy primitives globally.

Initial adopters: global Overview, Sessions list, Inbox, Settings, Host details,
Catalog/Tools panels and Files tree/selected-file chrome. The global Overview
and Files were gated with 1440px dark/light before/after screenshots before
rolling to the other routes. Project pages, shared shell header/sidebar and
recovery-state presentation remain with their respective owners. Their owners
can adopt these same primitives without adding another style or state system.

P2a adds device-local working columns and persistent terminal/editor hosts below.
Server-backed layout synchronization, Hosts redesign and recent results remain
follow-ups, not implied capabilities of this shell.

### P2a working columns and resource ownership

The new shell's `WorkingArea` owns up to three tabbed columns. Terminal/page minimum
width is 320px; Files and Chat need 360px, Preview 400px. Below the combined minima,
one complete column exposes every tab without rewriting the saved desktop layout.
Move and resize use pointer controls and labelled keyboard controls; Fill/Restore
changes geometry only. Tabs use route-independent IDs; geometry never adds history.
Use shared surfaces, borders, type tokens and visible focus styles. On 320–390px
headers, keep the brand's intrinsic width; wrap or shrink the host independently.

Stable keyed view hosts retain Files/Chat components. `TerminalOwner` wraps the
reviewed registry and a body-level host layer: xterm, its input subscription and
socket stay attached to one host. Geometry waits for preedit to finish. Terminal
selection is mapped back from selected content to cells after reflow (including
wide characters); scrolled readers retain an anchored buffer line. Hidden views
stop expensive detail/artifact/delivery/Chat work. One shared status/auth watcher
per retained session and a bounded Files recheck remain; terminal output still drains.

Working terminals attach as observers, bound to the server's current SessionRun
and stored node/account. Control is explicit on the same socket and tmux client.
Input/control/resize carry the run and writer epoch; the existing writer arbiter
also fences automated delivery. Partial printable input blocks automation but permits an explicit human handover or release after confirmation; unacknowledged bytes and active composition still block. Arrow keys, mouse reports and terminal replies do not create a partial-input hint. Handover never clears or replays bytes. Both shells use bounded reason enums; granted and refused attempts audit the previous actor/channel, target run, new channel, reason and outcome without input text.
Recheck never replays input. Closing a placement never stops a session or releases
a retained writer with unresolved input. Clean hidden writers release automatically; partial input offers an inline Show chip. Other controllers use one inline toolbar chip. Explicit Detach
frees the attachment, warning about unresolved input. Auth/run loss seals input
and removes terminal content; restored unavailable views use neutral placeholders.

The column registry bounds references to 32, attachments/xterm hosts to eight,
visible terminals to four, and scrollback to 10,000 rows each. Eight file buffers
have 1 Mi UTF-16-unit limits for each draft/base version; conflict copies use the
same file-read limit. Dirty, composing and unresolved-input resources stay pinned. At the attachment budget, the least recently used clean hidden terminal releases control and detaches; returning reattaches with its saved scroll position. Protected input may exceed the attachment budget. Clean hidden terminals also expire after ten minutes. The open session Terminal rechecks the run and claims free control after a restart.
These are enforced registry limits, not a measured whole-browser memory budget.

Files use a shared canonical document owner, server file identity and a checkout
revision derived from pinned directory identities. Aliases focus the existing
editor. A replaced checkout cannot receive an old draft, even with identical
content. Concurrent writes preserve the original base and draft for resolution.
Drafts remain volatile until server Save succeeds; closing a dirty file asks,
pending saves refuse close, and one beforeunload guard covers retained work.
The broader P0 unsaved-form/durable-draft work remains separate.

`deviceLayoutStore.ts` is the device-only persistence seam. Versioned, strictly validated
presentation records are scoped to installation, immutable person ID and project
or private workspace. Sign-out/frame teardown removes their private file paths; person changes and refreshed authorization inventories remove obsolete scopes. Explicit archived-history links remain readable; only restored archived references are neutral placeholders. Workspace Files landing pages without a session bypass resource restoration and show their existing guidance. They store references/placement only. Read/write/quota
failures remain visible and keep the current in-memory layout. No server layout
sync, authority restoration, command replay or automatic process launch is added.
Chat stays read-only and uses these columns for its beside-terminal action.

## Status board
`/board` (owners) is the one page outside the shell: no sidebar or top bar, made for a 16:9 display read from across a room, and usable in an ordinary window. It follows the same tokens and theme, and the same principles; "healthy is quiet" matters most here.
- **Layout:** a header row of `BoardStat`s (Needs you, Working, Machines, Machine alerts, Inbox), then a 12-column, two-row grid filling the rest of the screen: Machines and Projects (4 columns), Running sessions (5, both rows), and Plan usage, Inbox and Recent alerts (3). The page never scrolls at 1440x900 or larger; each `BoardPanel` scrolls on its own (a focusable region named after it). Below `lg` the panels stack.
- **Phone header:** below `lg`, two rows: Needs you and Working first, then the title and **Details**. The shared `Popover` holds Machines, Machine alerts, Inbox, hostname/freshness and utility actions. Its trigger and utility controls are at least 44 px tall and wide. Offline and source-refresh warnings stay outside the disclosure; the desktop header is unchanged.
- **Type:** one step up at `2xl` (≥1536 px, so a 1920 display): rows 14 → 17, meta 12.5 → 15, panel titles 12 → 14 (uppercase), headline numbers 26 → 34. `Dot large` is 10–12 px.
- **Status:** a row with a problem gets a 2 px amber or red left border and its problem in words; a healthy row has neither. Numbers turn amber above 60% and red above 85%, as `Meter` does. Nothing a machine didn't report is shown as 0: it reads "–" or says why.
- **Primitives:** `BoardPanel`, `BoardStat` and `Dot large` in `ui.tsx`; everything else is the shared session, Inbox and usage vocabulary (`sessionState`, `groupLive`, `groupInbox`, `NowDoing`, `ProviderMark`).

## Verification
- `scripts/ux_harness.py` serves the current build against a **copy** of the live database. It has no broker, no secrets and its own run and publish folders, so nothing reaches sessions, DNS or Caddy. Default port 18751.
- `web/scripts/ux-shots.mjs` screenshots every page (dark and light, desktop and phone).
- `web/scripts/ux-a11y.mjs` runs axe-core (WCAG 2.2 A/AA) on every page in both themes. Result on 2026-10-18: **0 violations**.
- The Playwright e2e suite (`web/e2e`) covers the flows. Its axe gate (`27-ux-a11y` and `27-ux-a11y-mobile`, WCAG 2.2 A/AA, only xterm's own markup excluded) covers every page (the status board and Host with another machine picked included), every session tab, the palette, New session, the phone drawer and the empty Overview, in dark and light, at 1440×900 and on a phone. Result on 2026-10-23: **0 violations**; keyboard tests cover the drawer and `Tabs`.

The terminal accessibility gate (`35-terminal-a11y`, `35-terminal-output` and its phone counterpart) checks session output with xterm included in axe, keyboard entry and exit, saved preferences, scrollback snapshots, clipboard success and failure, and phone key-bar behavior. Real NVDA and VoiceOver testing remains a separate manual check.

Both shared axe helpers wait for fonts and finite animations to settle before sampling
colours. Infinite status animations are temporarily paused, not hidden or excluded.
This avoids sampling theme-transition colours between the accessible dark and light
tokens; colour-contrast remains enabled, with node colours and ratios saved on failure.

The Rynhult v3 theme pass (2026-10-24) passed the complete browser suite: 341
passed, one conditional temporary-CA test skipped. A follow-up passed 15 tests,
covering that CA path and the remaining screenshot refreshes. Desktop and phone
axe gates are clean in both themes. All 169 existing Guide PNGs were refreshed,
with one new sign-in image. Initial JavaScript is 171.79 KiB gzip (250 KiB budget).

## Rollback
The pre-redesign UI is tagged `pre-ux-redesign`. To roll back:

```
git checkout pre-ux-redesign -- web
cd web && npm ci && npm run build
```

accd serves the build from disk, so no restart is needed. The previous build is also archived at `/var/backups/acc/ui-static-pre-ux-redesign.tar.gz`.

### New-shell project pages

Under the mounted new shell, Projects uses a 20-row directory with Project,
Source repositories, Working locations and Activity columns. Search, host,
repository owner and archived filters use the existing authorized project/session
queries; no machine inventory is fetched to populate the host filter. Filters
update native controls synchronously and persist in the URL. Archive and Delete
remain secondary row-menu actions. The standard Projects component is unchanged.

Project Overview keeps notes on one compact line and puts section tabs directly
below the title. Sessions retains the existing Continue actions and paging.
Results and Working locations have their own tabs; the duplicate brief summary
is removed. Repository and authorized group-location details live in Working locations.
Existing Files, Preview, health information and project sections remain reachable.
Page styles are shell-scoped, using semantic palette tokens only. The directory
shows the registered repository and primary host that the current project API
provides; it does not invent multi-repository counts, completion claims or wider
member visibility. Recent results reuses Knowledge and its document viewer.


### New-shell polish round 3

The optional shell replaces its Inbox status banner with an open-count rail badge (zero hidden, visual 99+ cap, full accessible count and unavailable/stale wording). Inbox remains the destination for every notice category.

New-shell session pages use one primary line plus a tabs row. Title/provider/state and primary lifecycle controls remain visible; metadata and secondary actions use Session details. Delivery is an inline chip linking to Activity. Retained terminal controls render into the tab row through a portal owned by the same SessionPage; no terminal-owner acquisition, writer fencing or lifecycle behavior changes. Secondary terminal controls use the shared keyboard-accessible popover. Narrow panes wrap controls without widening the working column.

Hosts context links show names and connection dots only, pointing directly to the existing per-host view. Rich observations remain in the Hosts overview and existing host pages. Every rail item opens its primary page directly; Tools opens Catalog. Useful sub-navigation remains, with no intermediate open-page links. Grid and Overview hide their context columns. Retained working resources survive rail navigation. These are presentation changes only, with no permission or legacy-layout changes.

Shell follow-up: Grid adopts ShellPage/Workbar while keeping its flexible terminal area; Rail selection derives from whole route segments in authorized navigation, including nested session/project and Tools routes. Terminal key rows use coarse-pointer visibility rather than viewport width, so narrow desktop panes do not acquire touch chrome.

The context column reserves horizontal space beside its heading for collapse, rather than an empty band above it. Title and short stats form a compact header. Scoped lists use 36–40px desktop rows and at least 44px coarse-pointer targets; long content may wrap. Footer hints stay muted, and project titles retain their full accessible name and hover title.

Session panes in column layouts keep the compact primary line, 12px side gutters and the metadata title scale. New-shell Session views place Setup, Activity and Handoffs under More; the selected tab stays in the strip with the existing tab/panel IDs and keyboard behavior. Standard layout keeps its default desktop strip.

Registered terminal views grow into the remaining session pane height through the chat/terminal frame, retaining a 320px minimum on short viewports. Height changes use the existing slot/terminal resize observers and do not reacquire the terminal or change its writer.

### Terminal file gestures

Both terminal layouts capture paste/drop only when DataTransfer contains files;
text-only paste remains xterm-owned. Terminal uploads reuse the Files upload API,
limits and broker folder policy, with a ten-file gesture limit. The server binds
uploads to the person's current writer connection; the broker chooses unique,
sanitized paths under `.rynhult/uploads/`. Local Git info/exclude is updated only
when the session folder is the repository root; linked worktrees must have a
matching metadata backlink. Exclude files are opened without following symlinks,
and an exact whole-line match prevents duplicate rules.
The browser accepts only `.rynhult/uploads/` paths with ASCII letters, digits,
dots, underscores and hyphens in the filename. An invalid path refuses insertion
of the entire batch. Successful paths are relative to the session folder and go
through terminal input without a newline. Progress/refusals appear in a polite
status beside the toolbar; its tooltip explains the relative-path base.
