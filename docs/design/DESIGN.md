# Rynhult design system (v3, 2026-10-24)

Rynhult is an **Operate** surface (impeccable's term): a dense, calm control room used mostly from a MacBook, sometimes from a phone.
References: Linear (navigation, density, ⌘K), Vercel and Railway (status and project cards), GitHub (lists), Grafana (meters).
Everything visual is defined in `web/src/index.css` (tokens) and `web/src/components/ui.tsx` (primitives). Pages compose those; they never define colours, radii or type sizes of their own.

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
