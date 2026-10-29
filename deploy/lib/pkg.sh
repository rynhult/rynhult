# shellcheck shell=sh
# Which package manager this Linux uses, and the few things the installers ask of it (docs/LINUX-DISTROS.md).
# Sourced by deploy/setup.sh, deploy/install.sh and deploy/node/install.sh.
# Only apt (Debian, Ubuntu) is qualified. dnf (Fedora, RHEL), zypper (openSUSE) and pacman (Arch) are recognised so
# the installers can say so and plan in a dry run; anything else, they refuse.

# The first one found. apt first, so a Debian or Ubuntu machine behaves exactly as before.
pkg_manager() {
  for pkg_candidate in apt-get dnf zypper pacman; do
    if command -v "$pkg_candidate" >/dev/null 2>&1; then
      if [ "$pkg_candidate" = apt-get ]; then echo apt; else echo "$pkg_candidate"; fi
      return 0
    fi
  done
  return 1
}

pkg_qualified() { [ "$1" = apt ]; }

# This system's name for the record, from os-release.
# shellcheck source=/dev/null
pkg_system() { (. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-${NAME:-Linux}}") || echo Linux; }

# A Debian package name in this manager's naming.
pkg_name() {
  case "$1:$2" in
    dnf:gnupg) echo gnupg2 ;;
    zypper:gnupg) echo gpg2 ;;
    pacman:python3) echo python ;;
    dnf:sqlite3|pacman:sqlite3) echo sqlite ;;
    dnf:uidmap) echo shadow-utils ;;
    zypper:uidmap|pacman:uidmap) echo shadow ;;
    pacman:gh) echo github-cli ;;
    zypper:nodejs) echo nodejs-default ;;
    zypper:npm) echo npm-default ;;
    *) echo "$2" ;;
  esac
}

# Updates the package lists before an install. Only apt needs it: dnf and zypper refresh stale metadata themselves,
# and on Arch a list refresh without a full upgrade (pacman -Sy) is a partial upgrade, which Arch doesn't support.
pkg_refresh_command() {
  case "$1" in
    apt) echo apt-get update -q ;;
  esac
}

# The command that installs packages without prompting or optional extras, one word per line.
pkg_install_command() {
  case "$1" in
    apt) printf '%s\n' env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends ;;
    dnf) printf '%s\n' dnf install -y -q --setopt=install_weak_deps=False ;;
    zypper) printf '%s\n' zypper --non-interactive --quiet install --no-recommends ;;
    pacman) printf '%s\n' pacman -S --needed --noconfirm ;;
    *) return 1 ;;
  esac
}

# What a person types to install them by hand.
pkg_hint() {
  case "$1" in
    apt) echo "sudo apt install" ;;
    dnf) echo "sudo dnf install" ;;
    zypper) echo "sudo zypper install" ;;
    pacman) echo "sudo pacman -S" ;;
  esac
}
