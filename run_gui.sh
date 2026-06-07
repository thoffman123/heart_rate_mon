#!/usr/bin/env bash
# Launches the Polar H10 monitor GUI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "Virtual environment not found. Run ./setup.sh first." >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/monitor_gui.py"
