# View registry

The production frame retains session terminals and file editors in working columns.
`components/shell/WorkingArea.tsx` owns `components/terminalOwner.ts`'s
`TerminalOwner` and `lib/documentRegistry.ts`'s `DocumentRegistry`. A column is a
placement, not a resource owner. Moving, resizing or filling a view preserves its
terminal/editor DOM. Grid retains its separate `Terminal` panes and limits.

## Terminal lifetime

`RegisteredTerminal` obtains the authorized current run from
`GET /api/sessions/{id}/terminal-reference`, then registers a geometry slot with
`TerminalOwner`. The owner lazy-loads `TerminalHostLayer`, creates a fenced host
and acquires a `ViewRegistry` reference. Identity includes installation, person,
session, run, node and account, serialized without delimiter ambiguity. The
production owner uses the browser origin for installation identity; the server
separately validates the current run. References never grant authorization.

`TerminalHostLayer` keeps each host in one stable body-level layer, batches slot
geometry reads and tracks resize, scroll and visual viewport changes. The shared
runtime owns xterm, FitAddon, links, websocket and input/selection subscriptions.
A pending asynchronous host import cannot acquire a slot after its owner unmounts.
`WorkingArea` sweeps every 30 seconds and shuts down terminals and documents on
teardown; its person key prevents resources crossing sign-in boundaries.

| Registry operation | Behavior |
| --- | --- |
| `acquire(reference)` | Shares a resource or creates an initially hidden attachment; defaults to `p2-observer`. Capacity may defer attachment. |
| `registerSlot(id, element)` | Returns idempotent cleanup that hides the host without stopping the session. |
| `place(viewId, slotId)` | Selects the canonical placement; duplicate views share the attachment. |
| `setVisibility(viewId, visible)` | Only the canonical placement controls host visibility and keyboard targeting. |
| `closeView(viewId)` | Removes a view without stopping the CLI; draft guards belong to the caller. |
| `inspect(resourceKey)` | Copies identity, attachment/waiting state, canonical view, visibility and pins. |
| `setPins(resourceKey, pins)` | Updates dirty, unsent or uncertain-operation protection and trims eligible overflow. |
| `sweep()` | Detaches eligible hidden resources after ten minutes and retries waiting attachments within budget. |
| `dispose(resourceKey, reason)` | Explicit release refuses protected work; confirmed input discard and revocation/teardown have distinct disposal reasons. |
| `shutdown()` | Disposes owned resources and refuses subsequent acquisition. |

Defaults allow four visible and eight attached terminals, including hidden ones,
with 10,000 scrollback rows per terminal. Acquisition evicts the least recently
used eligible hidden attachment first. If all eight are protected, it permits one
extra attachment and marks a protected resource for eviction when safe. Further
views wait; attachments never exceed nine. Active writers, composition, dirty
work and unresolved input protect resources from idle eviction. These are counts,
not measured heap guarantees.

## Control and reconnection

Production hosts attach read-only, bound to `run_id`. Explicitly opening a
Terminal tab can request free control; restoring an arrangement cannot. A held
terminal requires **Control terminal** or **Take control…**, a reason and
confirmation. The server owns single-writer arbitration and validates the current
run and access; the runtime's writer token is a local acknowledgement, not an
independent authority grant. Partial input is never cleared or replayed by handover.
Automated messages remain blocked by partial input.

Hidden registered terminals release control when they have no pending/partial
input or composition. Browser-tab hiding takes effect after 1.5 seconds. Hidden
attachments keep reading output without a keyboard target. **Detach terminal**
releases an attachment; unresolved input requires confirmation. **Recheck terminal**
creates a fresh read-only attachment. Returning to an explicitly opened terminal
or restarting its run can recheck automatically and request free control, never
steal it. **Open as text** and copy use the current run's retained browser buffer.

The separate `PersistentTerminals.tsx` provider/slot integration remains gated by
`VITE_P2_TERMINAL_HOST=1` for the `Terminal` presentation. Its nonce-bound
`run-observer-v1` protocol uses read mode, no take, fixed observer geometry and
explicit rechecking after disconnect. This qualification path is distinct from
the production `WorkingArea` owner. It is not required to enable working columns.
The server checks run identity and access before/after broker attachment and
periodically thereafter; sign-out/revocation closes tracked sockets immediately.
Out-of-band process replacement without updating `SessionRun` is not a native
process lease guarantee.

## Documents and saved arrangements

`DocumentRegistry` keeps one canonical editor per authorized placement/generation,
with up to eight base/draft pairs of at most 1 Mi code units each (32 MiB bounded
text). It stores drafts in memory only. Dirty close, save conflicts and unavailable
files keep their explicit review/recovery controls.

`workingLayout.ts`, `workingSerial.ts` and `layoutSync.ts` handle up to three
columns, saved authorized references and revision conflicts. Personal arrangements
are scoped by project, device and viewport. File references are opaque and access
is checked on restore. Saved layouts never contain drafts, output or control grants.
Offline pending arrangements may be retained on the device and are removed at
sign-out. Group-project arrangements remain local to the visit. See
[working views](/User-Guide/New-Layout#arrange-your-working-views) for controls,
cleanup limits and conflict choices.

## Focused verification

Run the bounded registry checks from the repository root:

```sh
node --experimental-strip-types --test web/scripts/view-registry.test.mjs web/scripts/xterm-composition.test.mjs
```

Browser checks use the normal `web/playwright.config.ts`, never an extra config.
The `view-registry` project uses the isolated fixture under
`web/e2e/fixtures/view-registry/` with no API proxy. It tests placement, sharing,
observer policy, hidden output/input, cleanup, selection and composition handling.
The opt-in routed-host check is `terminal-host.spec.ts`, built with
`VITE_P2_TERMINAL_HOST=1` and run with `ACC_E2E_P2_TERMINAL_HOST=1` in
`terminal-chromium`. Production session-owner and working-column journeys use the
ordinary frame. Select only the needed spec/project and its setup, with one worker;
do not start the stateful chain for a focused check. Synthetic Chromium and Safari
UA checks do not qualify native IME, physical Safari or multi-host performance.
