#!/usr/bin/env bash
# shellcheck source=/dev/null
. "$(dirname "$0")/../deploy/lib/acc-env.sh"
# Secret scan (history + working tree) and dependency vulnerability scan for the ACC repo.
# Requires gitleaks and osv-scanner on PATH (or in ACC_TOOLS); verify their releases independently.
# osv-scanner sends only package names and versions to osv.dev.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -n "${ACC_TOOLS:-}" ]]; then
    GITLEAKS="$ACC_TOOLS/gitleaks"
    OSV_SCANNER="$ACC_TOOLS/osv-scanner"
else
    GITLEAKS=$(command -v gitleaks) || { echo "Install gitleaks or set ACC_TOOLS" >&2; exit 1; }
    OSV_SCANNER=$(command -v osv-scanner) || { echo "Install osv-scanner or set ACC_TOOLS" >&2; exit 1; }
fi
"$GITLEAKS" git . --redact --no-banner
"$GITLEAKS" dir . --redact --no-banner
"$OSV_SCANNER" scan source --lockfile uv.lock --lockfile web/package-lock.json
echo "security-check: clean"
