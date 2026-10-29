# macOS standalone, slice 1: test plan for demo-mac

To be run later as a **watched session** on the Mac node `demo-mac` (Apple silicon, logged in to its graphical
session as the owner). It qualifies what Linux could only check by fixtures:
- the portable-main bundle (`desktop/build_mac_main.py`);
- the Keychain adapter (`desktop/mac_keychain.py`);
- the start-at-login LaunchAgent (`desktop/mac_launchd.py`);
- tmux recovery on macOS paths (`desktop/mac_tmux.py`);
- the gate in front of all of them (`desktop/mac_gate.py`).

Rules for the session:
- Run every command as the ordinary owner. **No `sudo`** and no administrator prompt. Part E is the exception: it
  is opt-in and runs only after the owner answers the staff question.
- Never run Rynhult's agent tools (`acc-*`, `rynhult*`) and never talk to the live Rynhult. Everything here is
  local to the Mac.
- No account, LaunchDaemon, firewall, TCC or system setting changes. Parts B and D make one test Keychain item and
  one test LaunchAgent, and remove them again.
- Record each step's output in `$LOG` and stop at the first unexpected result. Paste the log into the session's
  handoff; it holds no secrets (the Keychain test secret is random and deleted).

## 0. Setup

```sh
R="$HOME/rynhult-desktop-macos-standalone-1"   # checkout of branch desktop-macos-standalone-1
cd "$R" && git log -1 --format='%H %s'        # record the commit under test
LOG="$HOME/Desktop/mac-standalone-$(date +%Y%m%d-%H%M).log"; exec > >(tee -a "$LOG") 2>&1
sw_vers; uname -m                               # expect arm64, macOS 13 or later
command -v uv tmux node npm git; tmux -V        # all present; tmux 3.3 or later
id -u; id -Gn                                   # the owner's UID and groups (expect staff among them)
```

Expected: arm64 and every tool found. If `uv`, `tmux` or Node is missing, stop: this plan installs nothing.

## A. Unit tests and portable-main bundle

```sh
uv run --no-project --python 3.12 --with pytest==8.3.5 python -m pytest -q desktop/tests/test_mac_standalone.py
```
Expected: all tests pass on macOS too (the tmux test runs against Homebrew tmux).

```sh
(cd web && npm ci && VITE_DESKTOP=1 npm run build -- --configLoader runner --outDir ../desktop/web-dist)
desktop/build-runtime.sh                        # downloads the pinned runtime; prints 3.12.15 and its size
OUT="$HOME/Rynhult Build Test"                  # a space in the path on purpose
rm -rf "$OUT"; desktop/runtime/python/bin/python3 desktop/build_mac_main.py build --out "$OUT"
desktop/runtime/python/bin/python3 desktop/build_mac_main.py verify "$OUT/RynhultMain"
du -sh "$OUT/RynhultMain"; file "$OUT/RynhultMain/desktop/runtime/python/bin/python3"
codesign -dv "$OUT/RynhultMain/desktop/runtime/python/bin/python3" 2>&1 | grep -E 'Signature|flags'
find "$OUT/RynhultMain" -perm -g+w -o -perm -o+w | head   # expect no output
```
Expected:
- `build` prints the bundle path and file count, and `verify` prints `"verified": true`.
- `file` says `Mach-O 64-bit executable arm64`, and `codesign` shows an ad-hoc signature (`Signature=adhoc`).
- Nothing is group- or world-writable.

Start the portable main headless and stop it with EOF, the way the app does:

```sh
TOKEN=$(openssl rand -hex 32); DATA=$(mktemp -d "$HOME/rmain.XXXX"); chmod 700 "$DATA"
printf '{"token":"%s","data_dir":"%s","static_dir":"%s"}\n' "$TOKEN" "$DATA" "$OUT/RynhultMain/desktop/web-dist" \
  | ( "$OUT/RynhultMain/bin/rynhult-main"; echo "exit $?" ) &
sleep 15; jobs -l
```
Expected:
- Within 15 seconds one JSON line `{"origin": "http://127.0.0.1:<port>", "proof": ...}` appears, followed by
  `desktop_main_stopped` and `exit 0`. stdin closes after the single line, so the main shuts down on its own.
- Nothing is written inside `$OUT/RynhultMain`. Check with
  `desktop/runtime/python/bin/python3 desktop/build_mac_main.py verify "$OUT/RynhultMain"` again: it must still
  report verified.

Relocation: `mv "$OUT/RynhultMain" "$HOME/Rynhult Moved Main"` and repeat the start above with the new path. Expect
the same result, and `verify` still passes at the new location.
Cleanup: `rm -rf "$DATA" "$HOME/Rynhult Moved Main" "$OUT"`.

## B. Keychain adapter (the owner's login Keychain)

The real entry point (`owner_store`) is gated and must refuse here, before setup:

```sh
cd "$R/desktop"
../desktop/runtime/python/bin/python3 -c 'import os, mac_keychain; mac_keychain.owner_store(os.getuid())'
```
Expected: `mac_gate.Blocked: Desktop execution is not provisioned; run the one-time setup first`.

The adapter itself, with a test item that is deleted at the end:

```sh
SECRET=$(openssl rand -base64 24)
../desktop/runtime/python/bin/python3 - "$SECRET" <<'PY'
import os, sys, mac_keychain
store = mac_keychain.KeychainStore(os.getuid())
store.put('plan-test', sys.argv[1]); assert store.get('plan-test') == sys.argv[1]
store.put('plan-test', 'replaced'); assert store.get('plan-test') == 'replaced'   # -U updates in place
print('roundtrip ok')
PY
security find-generic-password -s com.rynhult.desktop -a "$(id -u).plan-test" | grep -E 'acct|svce|labl'
security dump-keychain -a "$HOME/Library/Keychains/login.keychain-db" 2>/dev/null \
  | grep -A25 '"svce"<blob>="com.rynhult.desktop"' | grep -E 'applications|/usr/bin/security|description'
```
Expected:
- `roundtrip ok`; the account is `<uid>.plan-test` and the label `Rynhult-Desktop`.
- **Record the access list.** A trusted-application entry for `/usr/bin/security` is the known pilot limit: any
  process of this user that runs `security` may read the item. Note whether macOS prompted at any point.

Secrets never appear in the process table:

```sh
( for i in $(seq 1 200); do ps -axww -o args; done ) > /tmp/rps.txt &
../desktop/runtime/python/bin/python3 -c "
import os, mac_keychain
s = mac_keychain.KeychainStore(os.getuid())
[s.put('plan-test', 'NEEDLE-%d-xyzzy' % i) for i in range(40)]"
wait; grep -c 'NEEDLE-\|TkVFRExFL' /tmp/rps.txt; rm /tmp/rps.txt   # expect 0
```
Expected: `0` (neither the secret nor its base64 form, which starts `TkVFRExFL`).

Also check that `security -i` left no history: `ls -la ~ | grep -i security` (expect nothing new).

Cleanup:

```sh
../desktop/runtime/python/bin/python3 -c "import os, mac_keychain; print(mac_keychain.KeychainStore(os.getuid()).delete('plan-test'))"
security find-generic-password -s com.rynhult.desktop -a "$(id -u).plan-test"; echo "exit $?"
```
Expected: `True`, then `exit 44` (the item is gone).

## C. tmux on macOS paths

```sh
cd "$R/desktop"; grep -E 'daily_clean_tmps' /etc/defaults/periodic.conf    # record: is /tmp cleaned, which age, sockets?
B=$(mktemp -d /tmp/mt.XXXX); mkdir -p "$B/run/rynd-501"
../desktop/runtime/python/bin/python3 - "$B" <<'PY'
import json, os, signal, subprocess, sys, time, mac_tmux
value = {'run_dir': sys.argv[1] + '/run', 'agent': 'rynd-501', 'agent_uid': os.getuid()}
env = {**os.environ, 'TMUX_TMPDIR': str(mac_tmux.tmpdir(value))}
tmux = lambda *a: subprocess.run(['tmux', '-L', mac_tmux.LABEL, '-f', '/dev/null', *a], env=env, capture_output=True, text=True, check=True)
print(json.dumps(mac_tmux.recover(value, mac_tmux.LABEL)))
tmux('new-session', '-d', '-s', 'acc-plan', 'sleep 120'); pid = int(tmux('display-message', '-p', '#{pid}').stdout)
alive = mac_tmux.recover(value, mac_tmux.LABEL); print(json.dumps(alive)); assert alive['sessions'][0]['name'] == 'acc-plan'
os.kill(pid, signal.SIGKILL); time.sleep(1)
again = mac_tmux.recover(value, mac_tmux.LABEL); print(json.dumps(again)); assert again['removed_stale']
print('recovery ok', alive['socket'])
PY
rm -rf "$B"
```
Expected:
- Three JSON lines, then `recovery ok`.
- The socket is at `$B/run/rynd-501/tmux/tmux-<uid>/rd`: macOS tmux honoured TMUX_TMPDIR with `-L`.
- `legacy.state` is `absent`, at `/private/tmp/tmux-<uid>/rd`.

Path limit with the real install location (no files are made):

```sh
../desktop/runtime/python/bin/python3 -c "
import mac_setup, mac_tmux
v = {**mac_setup.locations($(id -u)), 'agent_uid': 59999}; p = mac_tmux.socket_path(v, mac_tmux.LABEL)
print(len(str(p).encode()), p)"
```
Expected: a length under 104, and the path begins `/Library/Application Support/Rynhult Desktop/Owners/`.

## D. Start-at-login LaunchAgent

The real install is gated and must refuse before setup:

```sh
cd "$R/desktop"
../desktop/runtime/python/bin/python3 mac_launchd.py plan --app "/Applications/Rynhult Desktop.app" \
  || ../desktop/runtime/python/bin/python3 -c "
import mac_launchd, pwd, os, json
print(json.dumps(mac_launchd.plan(pwd.getpwuid(os.getuid()), 'install', mac_launchd.check_executable(
  __import__('pathlib').Path('/Applications/Rynhult Desktop.app/Contents/MacOS/rynhult-desktop'))), indent=1))"
../desktop/runtime/python/bin/python3 mac_launchd.py install --app "/Applications/Rynhult Desktop.app" --apply; echo "exit $?"
```
Expected:
- The plan prints the agent: `LimitLoadToSessionType` Aqua, `KeepAlive` false and the logs folder. If the app
  isn't installed, the plan through `check_executable` is enough.
- The `--apply` attempt fails with `exit 1`, blocked (not provisioned), and writes nothing:
  `ls ~/Library/LaunchAgents/com.rynhult.desktop.app.plist` must report no such file.

That launchd accepts the generated keys, with a throwaway label and `/bin/echo` (removed again):

```sh
../desktop/runtime/python/bin/python3 - <<'PY'
import os, pwd, pathlib, mac_launchd
owner = pwd.getpwuid(os.getuid())
value = mac_launchd.agent(owner, pathlib.Path('/Applications/Rynhult Desktop.app/Contents/MacOS/rynhult-desktop'))
value.update(Label='com.rynhult.desktop.plan-test', ProgramArguments=['/bin/echo', 'started-at-login'],
             StandardOutPath='/tmp/rynhult-plan-test.log', StandardErrorPath='/tmp/rynhult-plan-test.log')
pathlib.Path(os.path.expanduser('~/Library/LaunchAgents/com.rynhult.desktop.plan-test.plist')).write_bytes(mac_launchd.render(value))
PY
P=~/Library/LaunchAgents/com.rynhult.desktop.plan-test.plist
plutil -lint "$P"
launchctl bootstrap "gui/$(id -u)" "$P"; sleep 2; cat /tmp/rynhult-plan-test.log
launchctl print "gui/$(id -u)/com.rynhult.desktop.plan-test" | grep -E 'state|program|session|runs|last exit'
launchctl bootout "gui/$(id -u)/com.rynhult.desktop.plan-test"; rm -f "$P" /tmp/rynhult-plan-test.log
launchctl print "gui/$(id -u)/com.rynhult.desktop.plan-test"; echo "exit $?"
```
Expected:
- `plutil` reports OK and the log says `started-at-login` (RunAtLoad ran once).
- `launchctl print` shows session type Aqua, `runs = 1` and last exit 0, and doesn't restart it (KeepAlive false).
- After bootout, `print` fails (`exit 113`, not found).
- Record whether System Settings → General → Login Items listed it under "Rynhult" (AssociatedBundleIdentifiers)
  while it was loaded.

## E. After the owner's staff decision only (opt-in, needs the administrator)

Skip unless the owner has answered finding 3 of `docs/reviews/2026-10-25-review-desktop-phase1-mac.md`. Then:
1. Run `desktop/tests/mac-phase1-smoke.sh` exactly as `desktop/README.md` describes, with its typed confirmations.
2. With the account provisioned:
   - `../desktop/runtime/python/bin/python3 -c 'import os, mac_gate; print(mac_gate.require_ready(os.getuid())["agent"])'`
     must either print the agent name, or refuse with "must never belong to staff" or "membership unknown". Record
     which, along with
     `dsmemberutil checkmembership -u <agent uid> -g 20` and `-g 80`.
   - If it passed, repeat part B through `owner_store`, part D with `--apply`, then `uninstall --apply`, and check
     the broker's launchd log for one `tmux recovery:` line with `"state": "absent"`. Then kill the agent's tmux
     server as the agent through the smoke script's stop path, restart the broker, and expect `removed_stale` true.
3. Uninstall through the smoke script's default uninstall, and confirm the gate refuses again.

## What to report

- Each part's pass or fail, with the log.
- The Keychain access list and whether any prompt appeared (B).
- The `periodic.conf` cleanup settings (C).
- The Login Items appearance (D).
- In part E, the staff and admin membership answers.
- Anything that needed a change to pass: stop rather than edit on the Mac.
