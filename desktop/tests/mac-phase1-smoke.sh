#!/bin/bash
# macOS coordinator smoke. Default: portable plan only, no administrator prompt.
# --install --package setup.pkg: typed consent, then macOS Installer authorization.
# --check: inspect installed dedicated account/broker, files and logged-in CLI PTYs.
# --uninstall --package uninstall.pkg: preserve work/account.
# --full-uninstall --package purge.pkg: separately confirmed account/data/runtime purge.
# Build matching packages using desktop/build_mac_setup_pkg.py; see the Guide.
set -euo pipefail
umask 077
REVIEW=''
cleanup() { [ -z "$REVIEW" ] || /bin/rm -rf "$REVIEW"; }
trap cleanup EXIT
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=python3
OWNER=$(id -un)
OWNER_UID=$(id -u)
OWNER_GID=$(id -g)
AGENT_ID=50000
MODE=plan
PACKAGE=''
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON=${2:?}; shift ;;
    --owner) OWNER=${2:?}; shift ;;
    --owner-uid) OWNER_UID=${2:?}; shift ;;
    --owner-gid) OWNER_GID=${2:?}; shift ;;
    --agent-id) AGENT_ID=${2:?}; shift ;;
    --package) PACKAGE=${2:?}; shift ;;
    --install) MODE=install ;;
    --check) MODE=check ;;
    --uninstall) MODE=uninstall ;;
    --full-uninstall) MODE=purge ;;
    --dry-run) MODE=plan ;;
    --help|-h) sed -n '2,7s/^# *//p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done
# Validate the requested identity through the same portable planner first.
PLAN=("$PYTHON" -I "$ROOT/desktop/mac_setup.py" install --owner "$OWNER" --owner-uid "$OWNER_UID" --owner-gid "$OWNER_GID" --agent-id "$AGENT_ID")
if [ "$MODE" = plan ]; then exec "${PLAN[@]}"; fi
[ "$(uname -s)" = Darwin ] || { echo 'Apply/check requires macOS; use the default dry run here.' >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo 'Run as the ordinary desktop owner; Installer handles authorization.' >&2; exit 1; }
[ "$OWNER_UID" = "$(id -u)" ] && [ "$OWNER" = "$(id -un)" ] || { echo 'Smoke must run as its selected owner.' >&2; exit 1; }
"${PLAN[@]}" >/dev/null
if [ "$MODE" = check ]; then
  exec "$PYTHON" -I "$ROOT/desktop/tests/mac_phase1_probe.py"
fi
[ -n "$PACKAGE" ] && [ -f "$PACKAGE" ] || { echo 'Provide the reviewed, matching --package file.' >&2; exit 1; }
AGENT=rynd-$OWNER_UID
case "$MODE" in
  install) REQUIRED="CREATE $AGENT"; echo "This creates a hidden separate account, folders and a LaunchDaemon. Review the package plan first." >&2 ;;
  uninstall) REQUIRED="UNINSTALL $AGENT"; echo 'The matching uninstall package must preserve account and work.' >&2 ;;
  purge) REQUIRED="DELETE $AGENT AND ITS DATA"; echo 'The matching purge package permanently deletes agent work/credentials and, if unused, the shared runtime. Owner profile/app remain separate.' >&2 ;;
esac
printf 'Type exactly "%s" to continue: ' "$REQUIRED" >&2
IFS= read -r ANSWER || ANSWER=''
[ "$ANSWER" = "$REQUIRED" ] || { echo 'Cancelled; no Installer or account operation started.' >&2; exit 1; }
# Work from a private immutable-by-agent snapshot while Installer is open.
REVIEW=$(mktemp -d "${TMPDIR:-/tmp}/rynhult-desktop-pkg.XXXXXX")
case "$PACKAGE" in /*) ;; *) PACKAGE="$(pwd)/$PACKAGE" ;; esac
/bin/cp "$PACKAGE" "$REVIEW/Reviewed-Execution.pkg"
PACKAGE="$REVIEW/Reviewed-Execution.pkg"
VERIFY=("$PYTHON" -I "$ROOT/desktop/build_mac_setup_pkg.py" --owner "$OWNER" --owner-uid "$OWNER_UID" --owner-gid "$OWNER_GID" --agent-id "$AGENT_ID" --verify "$PACKAGE")
if [ "$MODE" != install ]; then VERIFY+=(--uninstall); fi
if [ "$MODE" = purge ]; then VERIFY+=(--purge-agent --remove-runtime); fi
"${VERIFY[@]}" >/dev/null
# Open the reviewed package in Installer. No sudo or deprecated elevation API.
/usr/bin/open -W "$PACKAGE"
if [ "$MODE" = install ]; then
  "$PYTHON" -I "$ROOT/desktop/tests/mac_phase1_probe.py"
  exit 0
fi
"$PYTHON" -I - "$MODE" "$OWNER_UID" "$AGENT_ID" <<'PY'
import json, pathlib, pwd, subprocess, sys
mode, uid, agent_uid = sys.argv[1:]
prefix = pathlib.Path('/Library/Application Support/Rynhult Desktop')
assert not (prefix/'Config'/uid/'execution.json').exists(), 'Execution is still configured; Installer may not have completed'
assert not pathlib.Path(f'/Library/LaunchDaemons/com.rynhult.desktop.{uid}.plist').exists()
running = [row.split()[1] for row in subprocess.run(['/bin/ps', '-axo', 'uid=,pid='], capture_output=True, text=True).stdout.splitlines()
           if row.split()[:1] == [agent_uid]]
assert not running, f'Agent processes still run after uninstall: {running}'
if mode == 'purge':
    try:
        pwd.getpwnam('rynd-'+uid)
    except KeyError:
        pass
    else:
        raise SystemExit('Agent still exists; Installer may not have completed')
    import grp
    try:
        grp.getgrnam('rynd-'+uid)
    except KeyError:
        pass
    else:
        raise SystemExit('Agent group still exists')
    assert not pathlib.Path(f'/etc/ssh/sshd_config.d/100-rynhult-desktop-{uid}.conf').exists(), 'Remote Login denial remains'
    assert not (prefix/'Config'/uid).exists() and not (prefix/'Sockets'/uid).exists(), 'Owner config/socket folders remain'
    assert not (prefix/'Owners'/uid).exists(), 'Agent data remains'
    assert not (prefix/'Config'/uid/'identity.json').exists(), 'Identity receipt remains'
    assert not (prefix/'Runtime').exists(), 'Shared runtime retained; review other owner receipts'
# Installer records every package, the purge package included; forgetting receipts needs an administrator.
receipts = [r for r in subprocess.run(['/usr/sbin/pkgutil', '--pkgs'], capture_output=True, text=True).stdout.split()
            if r.startswith('com.rynhult.desktop.') and r.endswith('.'+uid)] if mode == 'purge' else []
print(json.dumps({'uninstalled':True,'agent_data_removed':mode=='purge','owner_profile_preserved':True,
                  'installer_receipts_left':receipts,
                  'forget_receipts':[f'sudo pkgutil --forget {r}' for r in receipts]}))
PY
