#!/bin/bash
# Start OTC Assistant on macOS.
# Keep this file in the same folder as otc_daily_gui.py, otc_daily.py, otc_chart.py and otc_zones.py.
# Run it from Terminal with:  ./run_otc_gui.sh   (once: chmod +x run_otc_gui.sh)
# To start it with a double-click in Finder, make a copy named "OTC Assistant.command".
#
# The first run creates a private Python environment in the .venv folder next to
# this file and installs the required packages there. Delete .venv to rebuild it.

cd "$(dirname "$0")" || exit 1

fail() {
    echo
    echo "$1"
    echo
    read -r -p "Press Enter to close..." _
    exit 1
}

# ---- Make sure the app files are here ----
for f in otc_daily_gui.py otc_daily.py otc_chart.py otc_zones.py; do
    [ -f "$f" ] || fail "otc_daily_gui.py, otc_daily.py, otc_chart.py and otc_zones.py must be in the same folder as this script."
done

# ---- Find Python 3.9 or newer: Homebrew, python.org, then whatever is on PATH ----
PY=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 9))" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[ -n "$PY" ] || fail "Python 3.9 or newer was not found.
Install it from https://www.python.org/downloads/ (or: brew install python), then try again."

# ---- Create the private environment on first run (or if it stopped working) ----
VENV_PY=".venv/bin/python"
if ! "$VENV_PY" -c "import sys" >/dev/null 2>&1; then
    echo "Setting up the Python environment, first run only..."
    rm -rf .venv
    "$PY" -m venv .venv || fail "Could not create the Python environment. See the messages above."
fi

# ---- Install the required packages on first run ----
if ! "$VENV_PY" -c "import PyQt6.QtWidgets, numpy" >/dev/null 2>&1; then
    echo "Installing required packages, first run only. This takes a minute..."
    echo
    "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
    "$VENV_PY" -m pip install --upgrade PyQt6 numpy || fail "Package installation failed. See the messages above."
fi

# ---- Start the app, detached so this Terminal window can be closed ----
# Errors after start-up are saved to error_log.txt by the app itself.
nohup "$VENV_PY" otc_daily_gui.py >/dev/null 2>&1 &
disown
exit 0
