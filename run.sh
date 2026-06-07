#!/usr/bin/env bash
# Convenience wrapper — runs heart_rate_mon.py using the project's .venv.
# All arguments are forwarded to the script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "Virtual environment not found. Run ./setup.sh first." >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/heart_rate_mon.py" "$@"
