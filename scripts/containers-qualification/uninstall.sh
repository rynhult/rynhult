#!/usr/bin/env bash
set -euo pipefail
kit_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 -I -B "$kit_dir/kit.py" uninstall "$@"
