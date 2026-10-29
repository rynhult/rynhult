#!/bin/sh
# SELinux file contexts for Rynhult's data, run and socket paths (Fedora/RHEL, openSUSE Tumbleweed; docs/LINUX-DISTROS.md).
# Adds persistent rules (semanage fcontext) and applies them (restorecon). It never changes the enforcing mode: no
# setenforce and no permissive domains, ever. Run as root by the installers, only where SELinux is enabled.
#   deploy/lib/selinux-labels.sh main|node [--dry-run]
set -eu
role=${1:-}
dry=${2:-}
case $role in main|node) ;; *) echo "usage: $0 main|node [--dry-run]" >&2; exit 2 ;; esac
run() { echo "+ $*"; [ "$dry" = --dry-run ] || "$@"; }

if ! command -v semanage >/dev/null || ! command -v restorecon >/dev/null; then
  echo "SELinux is enabled but semanage/restorecon are missing: install policycoreutils-python-utils" >&2
  [ "$dry" = --dry-run ] || exit 1
fi

# path regex, type. Executables systemd or people start: bin_t (a service started from var_t would be refused).
# State: var_lib_t. Runtime folders and sockets: var_run_t; accd's socket is httpd_var_run_t so a confined Caddy
# (httpd_t) may write to it (connecting also needs the rynhult policy module: deploy/selinux/rynhult.te).
rules() {
  if [ "$role" = main ]; then
    echo '/srv/acc/app/\.venv/bin(/.*)? bin_t'
    echo '/srv/acc/bin(/.*)? bin_t'
    echo '/srv/acc/data(/.*)? var_lib_t'
    echo '/srv/acc/git(/.*)? var_lib_t'
    echo '/srv/acc/run(/.*)? var_run_t'
    echo '/srv/acc/run-root(/.*)? var_run_t'
    echo '/run/accd\.sock httpd_var_run_t'
  else
    echo '/opt/acc/app/\.venv/bin(/.*)? bin_t'
    echo '/opt/acc/bin(/.*)? bin_t'
    echo '/var/lib/acc-node(/.*)? var_lib_t'
    echo '/srv/acc/updates(/.*)? var_lib_t'
    echo '/srv/acc/packages(/.*)? var_lib_t'
    echo '/srv/acc/run(/.*)? var_run_t'
  fi
  echo '/run/acc-broker(/.*)? var_run_t'
}

rules | while read -r spec type; do
  # -a adds a rule; on a rerun it exists already, so -m makes it the same.
  if [ "$dry" = --dry-run ]; then
    echo "+ semanage fcontext -a -t $type '$spec'"
  else
    semanage fcontext -a -t "$type" "$spec" 2>/dev/null || semanage fcontext -m -t "$type" "$spec"
  fi
done
for path in $(rules | while read -r spec _; do printf '%s\n' "${spec%%(*}"; done | sed 's/\\\././g' | sort -u); do
  [ "$dry" = --dry-run ] || [ -e "$path" ] || continue
  run restorecon -R "$path"
done
