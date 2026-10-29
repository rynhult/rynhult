# Desktop session slice: Linux and macOS

2026-10-25. **Developer source/package, not a signed release.** The Tauri host
bundles a Python main and a separately built React/xterm UI. A manual, one-time
administrator setup provisions a separate agent account and protected broker
socket. The app/main never elevates. Installed server entry points, defaults
and normal web builds do not enable this composition.

**Post-v1 work:** the v1 product is the self-hosted Linux server, browser/phone
web app and existing Linux/macOS/Windows-WSL nodes. Native apps follow v1 one at
a time: native Windows, macOS standalone, Linux desktop, iOS, then Android.
Existing desktop source is experimental groundwork, not part of v1, and rc.1
does not start the native Windows turn. See
[the scope decision](../docs/decisions/0073-v1-linux-server-scope.md).

See `docs/DESKTOP-APPS.md` → Phase 1 for the design, and the Guide's Desktop apps
page for setup, provider login, project consent, uninstall and limitations.

## Runtime boundaries

- Kernel-selected IPv4 loopback port, fresh 256-bit capability over private
  stdin, HMAC readiness proof and host-set HttpOnly SameSite capability cookie.
  Every HTTP/WebSocket path checks capability, Host and Origin. No capability
  goes into argv, renderer storage or the agent environment.
- Private `0700` profile at `$XDG_CONFIG_HOME/rynhult-desktop-spike` (default
  `~/.config/rynhult-desktop-spike`). A file lock prevents concurrent mains.
  Native first navigation sets a normal owner session/CSRF cookie. One local
  owner is bootstrapped before listening; profiles with other people are refused.
- `setup.py install --owner <login>` prints a JSON plan by default. Only
  `--apply` as administrator performs it, and only from root-owned, non-writable
  installed resources. Setup creates a new locked `rynd-<uid>` account, private
  home, projects/spool with named-owner ACLs, configuration and desktop-specific
  systemd socket/service. No sudoers or general privileged command helper.
- `broker.py` requires the protected systemd socket, the actual dedicated UID
  and exact owner peer UID. It clears inheritance and uses the shared broker,
  provider adapters and tmux. `execution.py` composes the shared app and enforces
  native project consent and Claude/Codex launch gates, including resume.
- Native sessions are **Not isolated · Separate agent account**. They share that
  account's files/credentials. Filesystem permissions still matter. No native
  fallback is allowed when provisioning is absent.
- Provider sign-in offers a copyable URL for the system browser, with selectable
  text when clipboard access is unavailable. External WebView navigation stays
  blocked; the app adds no generic shell/opener bridge.
- Providers can explicitly install a reviewed exact npm version of Claude/Codex
  through the unprivileged broker. Setup prerequisites include `pkill` and
  `crontab` (procps/procps-ng and cron/cronie), systemd, Git,
  tmux, ACL tools, Python 3, Node and npm. No system tool is installed by the app.
  Running sessions must stop before CLI changes. No automatic CLI updates.
- EOF shuts down/reaps the local main. The Tauri watchdog closes the window if
  the main dies. The separately provisioned broker/tmux retain running agents
  on ordinary app quit. Active-work quit confirmation is still pending.

## Build

Use the provided read-only web/node_modules link when working in an ACC worktree.
The separate output directory is intentional: server assets stay a normal build.

```sh
(cd web && node node_modules/typescript/bin/tsc -b)
(cd web && VITE_DESKTOP=1 node node_modules/vite/bin/vite.js build --configLoader runner --outDir ../desktop/web-dist)
desktop/build-runtime.sh
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
cargo build --locked --manifest-path desktop/src-tauri/Cargo.toml
desktop/package.sh
```

The existing phase 0 prerequisites include WebKitGTK 4.1, GTK 3, xdo,
Ayatana AppIndicator, librsvg and their development/pkg-config packages. This
session installs none of them. `build-runtime.sh` pins python-build-standalone
CPython 3.12.15 through uv's SHA-256 manifest and hash-locks Python dependencies.
It writes `desktop/runtime`; set `UV_CACHE_DIR` inside the worktree if all build
cache writes must stay there. Use a project-local `CARGO_HOME` for the same rule.

`package.sh` stages only the production desktop Python files, runtime,
desktop UI, shared server package, catalog, Guide and agent hooks/tools. Tests
and the GTK display shim are excluded. It normalizes resource permissions to
match setup's protected-tree check, builds an unsigned `.deb` and deletes staged
copies afterwards. The package must be installed by an administrator before
applying execution setup; an extracted developer package is not root-owned.

`desktop/package-rpm.sh` then repackages that `.deb` as an RPM with the same files at the same paths
(`desktop/rpm/rynhult-desktop.spec`; needs rpm-build and binutils). It is not qualified on any RPM distribution
yet: see docs/LINUX-DISTROS.md.

AppImage (a portable fallback, not isolation): `desktop/appimage/fetch-tools.sh` fetches the three tools Tauri's
AppImage bundler runs, at pinned releases checked against `desktop/appimage/tools.sha256`, into the checkout's own
tools folder; then `desktop/package.sh --appimage` builds with them and checks them again afterwards. It refuses to
build without them, so Tauri never downloads a tool itself (one of its defaults is a moving "continuous" release).
Linux packages use technical names without spaces (`rynhult-linux-spike`, resources in
`/usr/lib/rynhult-linux-spike`); the menu still shows "Rynhult Linux spike". An installation of an earlier spike
package needs its desktop execution setup run again from the new path.

Tauri locates packaged resources at `<prefix>/lib/rynhult-linux-spike` beside
`<prefix>/bin`; developer builds use this checkout. The host uses the bundled
interpreter unless `RYNHULT_SPIKE_PYTHON` explicitly selects a development one.
No Python, Node or package manager is needed merely to open the app; execution
has the prerequisites above.

## Verify

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q desktop/tests
DESKTOP_BROWSER_CHECK=1 PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_desktop_browser.py
DESKTOP_NATIVE_CHECK=1 DESKTOP_APP="$PWD/desktop/src-tauri/target/debug/rynhult-desktop-spike" PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q desktop/tests/test_native.py desktop/tests/test_native_sessions.py desktop/tests/test_webkit_layout.py
```

## macOS preparation (no Mac results)

Apple silicon is prepared for **macOS 13.0 or later**, a provisional spike floor,
not a qualified support promise. Use Xcode/command-line tools, account-local Rust
and Tauri CLI 2.12.4, uv 0.13.1 or later (build-hash support), and Node/web build
dependencies. Opening a finished package will not require those build tools.
Intel is deliberately refused: the tested `cryptography==50.0.2` pin has no usable
Intel Mac wheel. A wheel-only Intel resolution failed on Linux; building native
dependencies from source or choosing different versions needs a separate turn.

On the Mac, from the checkout root:

```sh
export PATH="$HOME/.cargo/bin:$PATH"
# Prepare web dependencies first if absent: (cd web && npm ci)
(cd web && VITE_DESKTOP=1 npm run build -- --configLoader runner --outDir ../desktop/web-dist)
desktop/build-runtime.sh
desktop/package.sh --unsigned             # .app + .dmg
# Or --unsigned --app-only for the faster local app test.
```

`build-runtime.sh` selects uv's
`cpython-3.12.15-macos-aarch64-none` (PBS build 20261013 in uv 0.13.1), whose
archive hash uv verifies. This is a native assembly, not a Linux-to-Mac runtime
cross-build. The Mac interpreter dynamically links `libpython3.12.dylib`, which
must remain; only Linux's unused shared libpython is removed. Mac symbols are
stripped with `strip -x`, never ELF's `--strip-unneeded`. Tk libraries and their
extension, tests, pip and headers are pruned, and hash-based bytecode is
precompiled. Stripped Mach-O code is re-signed ad hoc for local execution.

The Mac runtime lock is separate and constrained to the tested Linux versions.
Only `http-ece==1.2.1` uses a source distribution:
`http_ece-1.2.1.tar.gz`, SHA-256
`8c6ab23116bbf6affda894acfd5f2ca0fb8facbcbb72121c11c75c33e7ce8cff`.
It is pure Python. All other runtime dependencies require wheels. Its isolated
setuptools/wheel build environment is also hash-constrained by
`requirements-build.lock`; an unexpected source build or unhashed build
dependency fails. Regenerate without installing packages:

```sh
uv pip compile pyproject.toml --constraint desktop/requirements-linux.lock \
  --python-version 3.12 --python-platform aarch64-apple-darwin \
  --generate-hashes --only-binary :all: --no-binary http-ece \
  --no-header --no-annotate --output-file desktop/requirements-macos-arm64.lock
printf 'setuptools\nwheel\n' | uv pip compile - \
  --python-version 3.12 --python-platform aarch64-apple-darwin \
  --only-binary :all: --generate-hashes --no-header --no-annotate \
  --output-file desktop/requirements-build.lock
DESKTOP_MAC_LOCK_CHECK=1 PYTHONPATH=src python -m pytest -q \
  desktop/tests/test_packaging.py -k 'dependencies or verified_arm64'
```

The opt-in check downloads compatible public wheel/source artifacts and verifies
their bytes against the locks, but never installs or executes them. Its manifest
goes to `.acc/artifacts/desktop-macos-lock-artifacts.json`.

### Bundle, hardened runtime and credentials

`package.sh` stages the same main/UI/catalog/Guide resources as Linux, then applies
`tauri.package.conf.json` and `tauri.macos.conf.json`. Tauri places them under
`.app/Contents/Resources`. A mismatched runtime is rejected before staging.
Both successful and failed builds clean copied source/wiki staging, keeping the
actual packages. `--unsigned` is the default; it removes signing/notarization
credentials from the build subprocess and uses Tauri's `--no-sign`.
This means **no Developer ID/notarization**; ARM Mach-O executables still use
local ad-hoc signatures. Do not distribute these artifacts as a release.

The signed configuration enables hardened runtime. The entitlement plist is
intentionally **empty**:

| Permission/setting | Reason |
| --- | --- |
| `allow-unsigned-executable-memory` / `allow-jit`: omitted | CPython interprets bytecode; the host does not embed a JIT. WKWebView's own Apple-signed processes handle WebKit JIT. Real CFFI/ctypes callback paths still need Mac qualification; add an exception only to the demonstrated executable after evidence/review. |
| `disable-library-validation`: omitted | The helper signs the bundled Python executable, dylib and native extensions with the same identity before Tauri seals the outer app. Do not bypass validation to compensate for missing nested signatures. |
| App Sandbox / `get-task-allow`: omitted | The spike claims neither an execution sandbox nor a debug entitlement in distribution builds. |
| ATS `NSAllowsLocalNetworking=true` (Info plist, not an entitlement) | Allows the loopback HTTP main without enabling arbitrary remote HTTP loads. The literal loopback bind, capability cookie, Host/Origin checks and HMAC remain unchanged. |

For an owner-controlled **signed and notarized** trial, `package.sh --signed`
requires `APPLE_SIGNING_IDENTITY` identifying the installed Developer ID
Application certificate, plus one complete credential set:

| Method | Environment variables supplied by the owner |
| --- | --- |
| App Store Connect API | `APPLE_API_ISSUER`, `APPLE_API_KEY`, `APPLE_API_KEY_PATH` (private `.p8` outside the checkout) |
| Apple ID | `APPLE_ID`, `APPLE_PASSWORD` (app-specific password), `APPLE_TEAM_ID` |

No identities, passwords, certificates or keys are committed. Configure these
only in the owner's secure environment; the script does not print them. Missing
notarization configuration fails rather than silently producing a signed-only
package. The identity must already be usable from the owner's keychain when the
nested-runtime helper runs; Tauri's optional certificate-import environment
variables alone do not bootstrap that earlier step. Developer ID signing,
notarization/stapling, offline Gatekeeper and a
sealed-bundle verification still need the Mac and owner credentials.

### Rust and lifecycle checks

The default `native-host` feature still builds the normal Tauri app. The small
library target lets Linux check the **same** supervisor/transport modules without
Apple GUI build dependencies:

```sh
rustup target add aarch64-apple-darwin
cargo check --locked --target aarch64-apple-darwin --no-default-features --lib \
  --manifest-path desktop/src-tauri/Cargo.toml
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

The core target was checked on Linux. The full Mac host check was attempted but
stopped in the Objective-C dependency: Linux's `cc` has no Apple `-arch`/SDK
support. It is not a full macOS host build. Private stdin EOF and Unix child
reaping are shared with macOS; seven Rust tests include real graceful Drop/EOF
and readiness-failure cleanup. Actual Cmd+Q, last-window-close behavior and
WKWebView shutdown remain Mac tests.

### Coordinator smoke and VoiceOver

On a logged-in Apple-silicon desktop, run this bundle-only check **before**
installing the execution Runtime. Once that Runtime exists, the host prefers it;
use the separate phase 1 account/broker smoke for that installed configuration.

Run:

```sh
desktop/tests/mac-smoke.sh
# For a quick harness trial only (not the five-minute idle sample):
DESKTOP_IDLE_SECONDS=5 desktop/tests/mac-smoke.sh
```

It builds the UI/runtime and unsigned `.app`, launches three fresh temporary
profiles, waits for the host's authenticated-readiness log, checks the main's
bundled interpreter, samples startup/idle, and tests native quit, host SIGTERM
and host SIGKILL. Native quit uses AppKit `NSRunningApplication.terminate`
through JXA for the **specific launched PID**, not another app with the same
name. If macOS refuses it, the script fails; do not disable TCC or Gatekeeper.
The main must log graceful shutdown and its process must stop in every case.
Only owned processes are cleaned up, never name-matched WebKit processes.
Reports and logs go to `.acc/artifacts/desktop-macos-*`; private profiles are
deleted. The monitor's success, missing-readiness and missing-shutdown paths
are tested using real temporary Linux processes, not Mac results.

Startup measures **main readiness, not UI interactivity**. Idle covers the host
and observed descendants; independent WebKit XPC services may be missing.
Summed RSS double-counts shared pages and is **not Linux PSS**. Record a separate
Activity Monitor/`footprint` whole-app sample before comparing the 400 MiB gate.
The script does not claim that CPU/memory gate passed.

Manual VoiceOver/keyboard pass (owner, no agents or real credentials):

1. Open the locally built app on the Mac. Confirm the macOS Standalone title and
   Projects page; leave execution unprovisioned for this UI-only check.
2. Enable VoiceOver with Cmd+F5 (or the Mac's configured shortcut). Use VO+Right
   and VO+Left (VO is Control+Option) to traverse the native window and WebView.
   Record the spoken title, Projects heading, execution-setup note and buttons.
3. Use Tab/Shift+Tab and the project filter with artificial content. Verify a
   visible focus indicator, readable field names and reachable controls. Confirm you can leave the
   web content and return without a keyboard trap.
4. Use the heading/form rotor. Check reading order, help/error announcements and
   that errors are not announced repeatedly. Native owner bootstrap must skip the
   server setup-token screen; provision execution only with the separate reviewed Installer.
5. Check 200% zoom/reflow, larger text, reduced motion, contrast, IME/dead keys and
   copy/paste with artificial content. Record macOS, VoiceOver and WebKit versions.
6. Test physical Cmd+Q and separately closing the last window. Confirm what stays
   running and verify the main's shutdown log/PID; the macOS menu/close policy is
   not qualified merely by Linux's close-button result.
7. Repeat on battery and after sleep/wake and screen lock/unlock. Record stale
   UI/reconnect behavior. The shared main watchdog closes the window on main exit;
   app lock and Keychain work remain open.
8. Record pass/fail and sanitized evidence, including exact failures. Do not
   substitute this checklist or the Linux accessibility tree for spoken results.

## Linux qualification limits

The main suite contains broker/provider/consent/CLI-install tests using fixtures.
`desktop/tests` adds capability/bootstrap, singleton/EOF, setup dry-run and native
checks. The main suite’s real-CLI probes only run when explicitly selected from inside a
separate disposable test account (`DESKTOP_REAL_CLI_ACCOUNT`); absent CLIs or
login skip clearly. They inspect auth status only, with no billable prompt.

Native tests start their own Broadway display, private D-Bus/AT-SPI bus (no
service activation) and Chromium display client. Broadway supplies no XSettings
daemon: a test-only C shim sets screen resolution to 96 DPI after GTK init, so
WebKit does not inherit an invalid negative device scale. It is compiled using
existing `cc`, pkg-config and GTK headers in a temporary fixture directory, loaded
only by test processes and never bundled. The fake-session test supplies a
fixture companion through the existing development interpreter override; it
uses the actual main/broker/UI with fake provider CLIs and a test-owned tmux
socket. Account UID simulation and layout instrumentation exist only in that
fixture. Production has no manifest override or same-user fallback switch.

Reports/screenshots go to `.acc/artifacts`. Cleanup uses owned PIDs/pidfds and
the test tmux ownership watchdog, never process-name kills. Fixtures do not call
live Rynhult endpoints or borrow the worker's provider credentials.

## Remaining release work

Physical Linux/derivative setup and desktop qualification (automated rootless
Arch account/socket/ACL qualification passed below); real CLI auth/inference and recovery; signed distribution/updates;
Secret Service/app lock; active-session quit UX; verified local backup/restore;
physical GNOME/KDE, Orca, IME, clipboard, scaling, sleep/logout and resource tests.
macOS needs native qualification, Keychain/TCC and signed packaging; Windows needs SID/service/pipe
ACLs and a ConPTY terminal host without WSL. See this session's handoff.

Uninstall ends the agent's processes, removes its crontab and keeps the
account locked and expired; it preserves work and the account. Automatic re-adoption of that
account is refused; administrator-reviewed recovery/reinstallation is still
manual. Stop sessions and close the app before applying uninstall.


## macOS phase 1 source port

The branch adds `mac_setup.py`, `mac_broker.py`, `build_mac_setup_pkg.py` and
`tests/mac-phase1-smoke.sh`. Portable dry-run examples:

```sh
python3 desktop/mac_setup.py install --owner alex --owner-uid 501 --owner-gid 20 --agent-id 50001
python3 desktop/build_mac_setup_pkg.py --owner alex --owner-uid 501 --owner-gid 20 --agent-id 50001
desktop/tests/mac-phase1-smoke.sh --python python3 --owner alex --owner-uid 501 --owner-gid 20 --agent-id 50001
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q desktop/tests/test_mac_setup.py desktop/tests/test_mac_broker.py desktop/tests/test_mac_package.py tests/test_desktop_mac_probe.py
```

Only the package builder's `--build` on a Mac creates an unsigned Installer
package, including the production modules, repository license and third-party
notices. A native bundled runtime and desktop UI must already exist. The setup
program's `--apply` is root/macOS-only and refuses the source checkout. The
coordinator's smoke script asks before account creation, checks the package's
owner/action recipe and opens macOS Installer for authorization. Uninstall uses
a separate package; full agent purge/runtime removal is separately confirmed.

The Mac host prefers `/Library/Application Support/Rynhult Desktop/Runtime`
after installation, keeping the main/broker/UI/runtime coherent. Before setup,
its normal bundle or source fallback opens the unprovisioned window. Include
all Mac desktop modules if producing a standalone bundle without that runtime.
Do not reuse the old node installer's same-user LaunchAgent or elevation script.

The probe exercises a dedicated desktop broker, files and logged-in CLI version
output/PTY without inference. Its Linux unit fixture simulates UID ownership;
no macOS result is claimed. See the Guide's macOS section and this turn's handoff
for install/uninstall commands, Keychain limits and outstanding physical tests.

Broadway is GTK's own display backend with software rendering, not X11/Wayland
with a window manager or compositor. Real-session rendering/GPU paths, window
manager focus, IME, zoom, clipboard, tray and sleep/lock still need a real desktop
login. The AT-SPI check proves the tree, names, roles and focus states are
exposed. It does not prove what Orca speaks, its reading order, or event
delivery through the registry; that needs at-spi2-core and Orca on a desktop. The
.deb is unsigned and has not been checked for reproducible builds. It was never
installed system-wide here, only extracted into a scratch folder. No AppImage
was built: Tauri would download linuxdeploy into the account cache.

## macOS standalone slice 1 (Linux-built)

`build_mac_main.py` builds and verifies the portable-main bundle. `mac_keychain.py` is the credential interface,
with a fake and the Keychain adapter. `mac_launchd.py` handles the start-at-login LaunchAgent; `mac_tmux.py` puts
tmux on macOS paths and recovers it. All are gated by `mac_gate.py` (the staff/admin check). Tests:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q desktop/tests/test_mac_standalone.py
```

The Mac commands are in `tests/mac-standalone-plan.md`. See `docs/DESKTOP-APPS.md` ("Phase 2 macOS standalone").

## Remaining local-transport qualification

The child-exit watcher polls every 200 ms; it reduces a dead-child window but does
not authenticate a replacement listener on the same port between polls. Startup
HMAC and same-origin navigation do not provide continuous server identity. There
is also no single-instance/profile lock: do not run concurrent hosts against the
same profile. Before shipping execution or secrets, qualify authenticated
reconnect transport and explicit profile ownership. See the break-it round 5
review in `docs/reviews/2026-10-25-break-it-5.md` for the threat and required tests.

## Arch automated account qualification

The disposable rootless Arch/systemd qualification passed makepkg (unprivileged
builder), pacman installation/integrity, separate-account fake sessions, real PTY,
synthetic auth, cross-user denial, owner login persistence and uninstall. Repeat
with [tests/arch-phase1.md](tests/arch-phase1.md); see docs/LINUX-DISTROS.md for the
measured versions and derivative caveats. This is headless developer evidence,
not a physical GUI, real provider auth, derivative or production GO. Uninstall's
UID sweep was a verified-empty fixture no-op; forced termination remains open.

The recorded Arch run predates the 2026-10-26 setup/uninstall and packaging
hardening. Repeat the procedure with a fresh package; unit and packaging-fixture
tests do not replace that systemd/account qualification.
