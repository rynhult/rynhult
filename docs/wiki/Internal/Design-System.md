# Design system

Rynhult is an operations surface: dense, calm and legible, closer to Linear or Vercel than to a marketing site. Everything visual is defined once, in `web/src/index.css` (tokens) and `web/src/components/ui.tsx` (primitives).

## Principles
1. **Status first.** The first screen answers what needs me, what is running and whether the host is healthy.
2. **Healthy is quiet.** Green appears only as small dots; colour words and borders are for problems.
3. **Loading, error, stale and empty are different states.** Never show "nothing here" for data that hasn't arrived.
4. **Show provenance.** Every number says where it came from and how old it is.
5. **One way to do each thing.** One page frame, one header, one session row, one meter, one confirmation dialog.

## Tokens

The brand uses Charcoal and Slate dark surfaces, Frost and Snow light surfaces,
and Periwinkle interaction accents. The UI keeps its existing density and fonts.

| Token | Dark | Light | Use |
|---|---|---|---|
| `bg`, `panel`, `panel-2`, `panel-3` | #10191f, #15232b, #1c2e38, #263b46 | #f0f3f3, #ffffff, #e8eff3, #dae5ed | Surfaces |
| `fg`, `muted`, `faint` | #f2f5f4, #a4b4be, #93a7b4 | #10191f, #485d68, #526671 | Text |
| `accent`, `accent-strong` | #abb6ff, #939ef2 | #414fa8, #3d4c9e | Links, focus, primary actions |
| `ok`, `wait`, `bad`, `info` | #84ecb7, #ffd78d, #ffa0a0, #b6ddff | #086039, #794200, #aa1e24, #174f9e | Status |
| `idle` | #b9cfe1 | #50657a | Idle / stopped |
| `line-input` | #8da4b2 | #627987 | Essential field boundary |
| `accent-ink`, `status-ink` | #10191f | #ffffff | Text on solid action / count backgrounds |

Body text meets 4.5:1 on each surface; essential control boundaries and focus
meet 3:1. The darker light-theme status fills use white count text, while the
brighter dark-theme fills use Charcoal. Decorative dividers use `line` and
`line-strong`; they do not identify a control on their own. Status hues retain
or improve their stage-1 contrast, including tinted notes and badges.

`docs/design/DESIGN.md` records the full ratios and derivations. Run
`npm run test:contrast` in `web/` to check all surface pairs, tinted backgrounds,
primary-button hover ink, installed-app splash consistency and status
non-regression. The browser suite also measures solid Inbox count contrast.
Type is Inter and JetBrains Mono, self-hosted.

## Components
`Page` and `PageHeader`, `Card` and `CardHeader`, `Stat`, `Meter`, `Badge`, `Dot`, `Tabs`, `Segmented`, `Toolbar`, `SearchInput`, `Button` and `IconButton`, `Dialog` (focus trap, pinned footer, no accidental dismissal for forms), `useConfirm`, `QueryState`, `Empty`, `Skeleton`, `SessionList`, `Wiki`.

`Dialog` always focuses an in-dialog target, falling back to Close or the panel while asynchronous content loads. Tab recovers focus moved outside the panel; closing restores the original outside trigger even when React `autoFocus` moved it before the opening effect.

Session Activity uses `QueryState` for loading, initial error, empty and stale-with-error states. Below the `sm` breakpoint its metadata stacks above wrap-safe event content; desktop uses three columns with a shrinkable event track.

### Touch screens
On a coarse pointer (phones, tablets), every tap target is at least 44 × 44 px:
- the shared primitives grow on `pointer-coarse`: `Button`, `IconButton`, `Popover` triggers, `Tabs`, `Segmented`,
  `Input`, `Select`, `SearchInput`, and `CopyButton`'s button;
- `index.css` gives `summary` elements and checkbox or radio labels a 44 px minimum;
- links styled as controls (card actions, section indexes, breadcrumbs, list rows) carry `pointer-coarse:min-h-11`, and
  `pointer-coarse:min-w-11` where they can be narrow.

Desktop sizes are unchanged.

Exempt: links inside running text, and a diff's `Comment on line …` rows, which keep code-line height; the whole row is
the target.

`web/e2e/91-phone-pass-mobile.spec.ts` walks the main flows at 390 px, touch, in both themes. It fails on:
- anything that sticks out sideways (unless scrollable or clipped);
- smaller targets;
- an input covered by a fixed or sticky bar;
- with the keyboard open (a 470 px tall viewport), Add comment, Send to agent or Launch being off screen or covered.

## Checks
The UI is checked with axe-core against WCAG 2.2 AA on every page in both themes (0 violations at the last check), and screenshots of every page in dark, light and phone layouts.

`CopyButton` awaits clipboard writes and uses the one app-root `Announcer`; authentication/setup screens share it. On refusal or missing Clipboard API it selects the original text and displays manual-copy guidance. `CopyBlock` bounds and names its pre/code region. Shared Markdown rendering applies this policy to fenced code and wraps tables in named keyboard scrolling regions. Card headings and actions wrap on phones; radio segments use roving focus and machine selection uses a native select on phones or with many machines.

Guide heading links scroll after both the article and page tree are ready, so
a fast article response cannot leave a deep link at the top of the page.
