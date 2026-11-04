#!/usr/bin/env bash
set -euo pipefail
HL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HL_SCRIPT_DIR/install_macos_linux.sh" "$@"

