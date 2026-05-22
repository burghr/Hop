#!/usr/bin/env bash
# Sets up the venv on first run, then launches the app.
set -e
cd "$(dirname "$0")"

VENV=.venv

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment…"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -r requirements.txt
    echo "Done."
fi

exec "$VENV/bin/python" app.py
