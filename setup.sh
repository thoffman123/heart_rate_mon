#!/usr/bin/env bash
# Creates a .venv in the project directory and installs dependencies.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON=/opt/homebrew/bin/python3.11   # Tk 8.6, no deprecation warning

if [[ ! -x "$PYTHON" ]]; then
    echo "Python 3.11 not found at $PYTHON" >&2
    echo "Run: brew install python@3.11 python-tk@3.11" >&2
    exit 1
fi

if [[ ! -d "$VENV" ]]; then
    echo "Creating virtual environment at $VENV …"
    "$PYTHON" -m venv "$VENV"
else
    echo "Recreating virtual environment with Python 3.11 …"
    rm -rf "$VENV"
    "$PYTHON" -m venv "$VENV"
fi

echo "Installing / upgrading dependencies …"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet

echo ""
echo "Setup complete."
echo ""
echo "Run the GUI monitor:"
echo "  ./run_gui.sh"
echo ""
echo "Run the CLI monitor:"
echo "  $VENV/bin/python $SCRIPT_DIR/heart_rate_mon.py --help"
echo ""
echo "  # Scan for nearby BLE devices"
echo "  $VENV/bin/python heart_rate_mon.py --scan"
echo ""
echo "  # Stream HR to stdout (compact JSON, one line per reading)"
echo "  $VENV/bin/python heart_rate_mon.py"
echo ""
echo "  # Stream HR + pretty-print + battery level"
echo "  $VENV/bin/python heart_rate_mon.py --pretty --battery"
echo ""
echo "  # Stream to stdout AND broadcast on TCP port 5555"
echo "  $VENV/bin/python heart_rate_mon.py --tcp-port 5555"
echo ""
echo "  # TCP only (suppress stdout), auto-reconnect"
echo "  $VENV/bin/python heart_rate_mon.py --tcp-port 5555 --no-stdout --reconnect"
echo ""
echo "macOS Bluetooth permission:"
echo "  System Settings → Privacy & Security → Bluetooth → enable Terminal (or your IDE)"
