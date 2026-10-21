#!/usr/bin/env bash
# Runs the server under Gunicorn, the way the systemd unit and the container do.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
exec "$PYTHON" -m gunicorn -c deploy/gunicorn.conf.py proxy_server:app
