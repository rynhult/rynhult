# Throwaway terminal continuity comparison

This is a standalone experiment, not app code or a new Rynhult session. It imports the installed repo xterm ES module and applies the same in-memory composition compatibility transform as the app build (`web/scripts/xterm-composition.mjs`). It also serves the repo's FitAddon. No installed asset is modified.

From the repository root, with its locked web dependencies already installed:

```sh
PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright \
  web/node_modules/.bin/playwright test \
  --config docs/design/prototypes/continuity/playwright.config.mjs
```

`CONTINUITY_PYTHON` can select an existing Python with `websockets` installed; the default is `/srv/acc/app/.venv/bin/python`. No package installation is performed. The dedicated config belongs to this standalone experiment; it does not start the app's e2e harness or inherit any app setup projects. One Chromium worker runs this file only. Results and JSON attachments go to ignored `.acc/continuity-results/`.

Each test starts its own loopback HTTP server on an OS-assigned port and a child loopback websocket fake on another OS-assigned port. The fake seeds a sentinel and 120 numbered lines, then streams numbered lines every 25 ms. It records connection/close counts, input and resize frames. It never opens a shell, broker, PTY or session. Teardown closes the page, HTTP connections and the child by its own process handle.

## Comparisons

- **A / stable:** one absolute-positioned terminal host stays inside one fixed overlay parent. Only its geometry changes between Column 1, Column 2 and Expanded fill.
- **B / reparent:** the same host is physically moved with `appendChild`. This tests DOM reparenting, **not React portal reconciliation**. Moving a portal between parents may remount; that would need an additional prototype.
- Two raw diagnostic tests deliberately assert observed missing safeguards: A's resize loses the selection; B's blur clears the composition textarea. Their green result means the hazard reproduced, not that the raw approach is safe.
- Two guarded tests each perform **20 actual moves** while output streams, starting and committing one synthetic Japanese composition per move. A changes geometry immediately but postpones fitting until commit. B queues each requested move until commit, then physically reparents and restores focus. The test checks that no B move occurs before commit. Neither strategy commits/cancels the composition on behalf of the person.

Both guarded paths retain one xterm, socket and textarea; preserve the sentinel and all seeded/streamed history in order; keep a selected short sentinel; restore focus; and deliver each committed composition exactly once. Selection restoration records its buffer position/text across fit and commit. This is intentionally limited to the short single-line sentinel whose coordinates do not change on reflow: **wrapped/multiline selections are not qualified**.

Only the canonical host has a ResizeObserver. The test resizes inactive slots and asserts that neither fitting nor resize sends change. Work limits are one fit per animation frame, at most 21 fits/resize sends (initial + 20 moves), at most 42 observer callbacks, and at most 21 explicit placement geometry reads. CDP records browser layout/style totals, including xterm rendering, synthetic IME and streaming output; those totals are bounded at 240 plus twice the number of output chunks. This is a deterministic work-budget smoke test, not a frame-time or heap qualification.

## Limits

Synthetic `CompositionEvent`s do not prove native OS IME/candidate-window behavior. Real Mac/Windows/Linux IMEs, assistive technology, wrapped selections, touch keyboards, font changes, viewport scroll/zoom, clipping/stacking, access revocation, multiple hosts, React routing and provider acceptance are outside this experiment. Run the corresponding focused tests before any product claim. A websocket input receipt here proves only what the fake received.

Firefox and WebKit need their own projects/runs, engine-neutral rendering metrics (CDP is Chromium-only), and investigation of engine-specific raw diagnostic outcomes rather than assuming Chromium's loss pattern. Native IME needs manual OS testing in addition to synthetic events. Do not run another full browser project to exercise this experiment.

Recommendation and measured Chromium results: [continuity contract](../../continuity-contract.md#prototype-results).
