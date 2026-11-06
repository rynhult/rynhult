# View registry foundation

The registry foundation now has an **off-by-default, read-only P0 step-2 integration**. Build the web UI with `VITE_P2_TERMINAL_HOST=1` to qualify it; ordinary builds retain Terminal's existing mount/disposal and writable attach/take path. This build flag is latched for the page load, not a layout preference or permission. It does not enable the future writer arbiter or complete P2 layouts.

`PersistentTerminalProvider` lives above routed pages. It owns a person-scoped registry, one stable body-level layer, the 30-second idle sweep and stable React portals containing the existing Terminal presentation. Routed Terminal components register only geometry slots. Moving between Session, tabs and Grid changes a canonical placement; xterm, its input subscription and socket belong to the registry handle. Another simultaneous placement offers **Show shared terminal here**, without creating another attachment. Hidden observers keep draining output with keyboard targets disabled. **Disconnect** releases explicitly; rechecking after release makes a new observer connection and is not uninterrupted continuity. A background summary offers explicit release of hidden observers. Sign-out (including query-cache removal), authority-end socket codes and page teardown clear owned resources/listeners.

`web/src/lib/viewRegistry.ts` owns resource lifetime independently of view placement. Its identity includes installation, person, session, run, node and account, serialized without delimiter ambiguity. A duplicate view shares one resource; a new process incarnation is a different resource. References are not authorization grants.

| Operation | Behavior |
| --- | --- |
| `acquire(reference)` | Creates or shares one attachment, initially hidden, and returns a view ID/resource key. Default policy is `p2-observer`. Explicit acquisition can recreate an idle-detached resource; showing a detached view cannot silently reconnect it. |
| `registerSlot(id, element)` | Registers a placement target and returns idempotent cleanup. Cleanup hides its host without stopping the session or disposing the attachment. |
| `place(viewId, slotId)` | Selects the canonical view and moves the host's geometry. A different resource cannot occupy an already visible slot. Duplicate views do not create duplicate terminal writers. |
| `setVisibility(viewId, visible)` | Hides/shows that view without closing its connection or relinquishing an acknowledged writer. Only the canonical view controls the host. Hidden hosts are inert and cannot send keyboard input. |
| `closeView(viewId)` | Removes a view, retaining the resource until explicit release or eligible idle eviction. It does not Stop a CLI. Draft guards remain the caller's responsibility. |
| `inspect(resourceKey)` | Returns a copy of identity/state, view count, canonical view, visibility and pins. Detached placeholders explain that reconnection needs an explicit access recheck. |
| `setPins(resourceKey, pins)` | Supplies dirty, unsent and uncertain-operation pins from their actual owners. It neither stores drafts nor implements draft guards. |
| `sweep()` | Detaches eligible hidden resources after ten minutes on a monotonic clock, oldest first; drops metadata if no views remain. Also runs before new attachments are admitted. It owns no recurring timer; future integration may call it from one shared idle scheduler. |
| `dispose(resourceKey, reason)` | Explicit release refuses dirty/pending/composing work. Sign-out/revocation/shutdown forcibly discard the resource and its references. Disposal is idempotent. |
| `shutdown()` | Disposes all resources and refuses subsequent acquisition. Call on person/installation boundary changes. |

Default limits are four visible resources and eight attached xterm/socket resources, including hidden ones; xterm retains 10,000 scrollback rows. Budget exhaustion refuses the new operation without disturbing existing placements. Active writers, composition, dirty drafts, unsent input and uncertain sends prevent idle eviction. The 32 MiB draft budget belongs to the separate unsaved-work registry, not this terminal library. These counts are limits, not measured heap guarantees.

`TerminalHostLayer` in `web/src/components/terminalHostLayer.ts` appends each host exactly once to its fixed layer. It batches slot geometry reads before positioning hosts, observes canonical visible slots and follows resize/scroll/visualViewport changes. Hidden entries continue reading output/auth messages, but have no page polling or active keyboard target. Dispose the layer after registry shutdown to remove its observers/listeners/frame and root DOM. A pending Safari input-adapter import cannot create an editor/socket after disposal. Composition pin tracking runs before Safari's capture adapter, which deliberately stops event propagation.

The shared runtime owns xterm, FitAddon, link handling, websocket, bounded scrollback, input/selection subscriptions and its local writer acknowledgement. The local token is the acknowledged websocket, **not a new server-issued lease or authority epoch**. The registry passes an explicit policy:

- `p2-observer`: `mode=read&run=<SessionRun.id>`, no take, no input frames and no PTY resize frames. Restoration never claims control, even if free.
- `p1-claim-if-free`: the existing attach semantics, without automatic take. Standard builds retain their current separate Take control path. Registry hosts coalesce fitting and defer it while composing; legacy Terminal keeps its existing fit schedule.

The stable host preserves DOM and selection/IME state across placement changes. Selection restoration currently handles a single unwrapped selection only when the same selected text remains at the same cell coordinates after fitting, including wide Unicode cells. Wrapped/reflowed selections need content anchors before production P2 integration. No native OS IME, cross-engine overlay or multi-host performance qualification is implied by synthetic Chromium tests.

### Run-bound observer protocol

`GET /api/sessions/{id}/terminal-reference` resolves the authorized current running incarnation, a public server-instance nonce and node/account/person with `Cache-Control: no-store`. A saved reference cannot start or resume a process. A bound tty request must echo that nonce, specify read mode and no take; the server checks the current run, account, host and terminal name before broker attach, after its awaited handshake, during periodic access checks and before forwarding output when its last check is older than 250 ms. Output waits for that check, which runs off the event loop and queries only the newest run. Explicit logout/revocation still closes tracked sockets immediately. Observers cannot submit input or resize frames, including forged ones. A typed `terminal_observer_geometry` preflight pins the provider window to manual sizing before attaching. Ignore-size alone is insufficient without a writer. Older brokers that lack the preflight are refused before attach. Only writable broker attachments explicitly resize the provider window; read-only private client PTYs cannot size it. Observers keep the acknowledged grid size and scroll inside their slot rather than fitting/reflowing locally. The acknowledgement carries `protocol: run-observer-v1` and the exact run ID and nonce. The reference’s installation field is this opaque process-scoped server identity, deliberately replaced on server restart rather than inferred from a display hostname; references from another server incarnation are refused. Missing/mismatched acknowledgements fail closed; no legacy writer fallback is used. The persistent host’s text/copy controls use only its retained run-specific browser buffer, with an explicit limitation notice; they never fall back to the legacy session-bound snapshot API. Legacy writer claiming/input is unchanged in this step; broker window sizing is now explicit, and read-only views keep a fixed local grid. All observer connection closes discard cached output and require a fresh explicit Recheck, including network/restart closes.

These are server-tracked `SessionRun` checks, not a broker-issued native-process lease. Out-of-band tmux/process replacement without updating server state is not qualified by this slice. No layout synchronization, takeover, unified Chat/team arbitration, receipts, editor document registry or draft persistence is implemented. Shared file documents remain a follow-up: moving the current Files owner safely needs the separate draft/unsaved-work lane. Interactive P2 control waits for step 4; this integration offers observation only.

## Focused verification

```sh
node --experimental-strip-types --test web/scripts/view-registry.test.mjs web/scripts/xterm-composition.test.mjs
```

Run the registered `view-registry` project from `web/` through the main config:

```sh
ACC_E2E_PORT=19771 ACC_E2E_BASE=/tmp/continuity-tests \
ACC_E2E_PYTHON=/path/to/project-venv/bin/python \
PLAYWRIGHT_BROWSERS_PATH=/path/to/playwright-browsers \
  node node_modules/@playwright/test/cli.js test \
  --config playwright.config.ts --project view-registry --workers=1
```

Use your own port/base and an existing project Python. The main config starts its disposable app harness; the `view-registry` project has no setup/stateful dependencies. Its worker owns a separate ephemeral loopback Vite listener and closes it after the spec. The fixture lives under `web/e2e/fixtures/view-registry/`, has **no API proxy**, disables HMR, and is not a production build input or application route. Playwright owns the fake websocket PTY; the fixture cannot launch a Rynhult session. The main desktop project excludes this spec so it runs only in its registered project.

The focused browser spec covers 20 slot moves, observer policy, one attachment for duplicate views, hidden output/input, slot cleanup, revocation disposal, failure cleanup, wide-Unicode selection, cancelled lazy Safari initialization and Safari capture-order composition tracking (UA emulation, not native WebKit). Existing terminal page specs remain registered in their normal main-config projects. No extra Playwright configs are required. Runtime resources do not survive browser reload; a restored view remains an observer when the opt-in integration acquires it.

For the actual routed host test, build with `VITE_P2_TERMINAL_HOST=1`, then run `terminal-host.spec.ts` with `ACC_E2E_P2_TERMINAL_HOST=1` through the main configuration's `terminal-chromium` project. For each focused invocation select setup and exactly one terminal spec, with `--project=setup --project=terminal-chromium --no-deps --workers=1`. Rebuild without the flag for the existing `termfull`, `terminal-input` and `terminal-polling` specs. The new project registers those files without duplicate desktop execution. Never start the stateful project chain for a focused check.
