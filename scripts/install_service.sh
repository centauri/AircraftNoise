#!/usr/bin/env bash
# Installs the recorder as a systemd service with auto-restart, plus the
# watchdog timer. Run from the project directory on the Debian host:
#   sudo bash scripts/install_service.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON="$DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
for unit in planenoise.service planenoise-watchdog.service planenoise-watchdog.timer; do
  sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_USER|g" -e "s|__PYTHON__|$PYTHON|g" \
    "$DIR/scripts/systemd/$unit" > "/etc/systemd/system/$unit"
done
mkdir -p "$DIR/data" && chown "$RUN_USER" "$DIR/data"
systemctl daemon-reload
systemctl enable --now planenoise.service
systemctl enable --now planenoise-watchdog.timer
echo "Installed. Status:"
systemctl --no-pager --lines=0 status planenoise.service planenoise-watchdog.timer || true
echo "Watchdog needs: sudo visudo -> '$RUN_USER ALL=(root) NOPASSWD: /bin/systemctl restart planenoise.service'"
