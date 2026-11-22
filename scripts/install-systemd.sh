#!/usr/bin/env bash
# Install the systemd units for this checkout: fills in @INSTALL_DIR@ and enables the stack + timers.
#   sudo scripts/install-systemd.sh            # all units
#   sudo scripts/install-systemd.sh --public   # also the certificate renewal timer (public Grafana)
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
for f in deploy/systemd/*.service deploy/systemd/*.timer; do
  sed "s#@INSTALL_DIR@#$DIR#g" "$f" > "/etc/systemd/system/$(basename "$f")"
  chmod 644 "/etc/systemd/system/$(basename "$f")"
done
ln -sf "$DIR/bin/projctl" /usr/local/bin/projctl
systemctl daemon-reload
systemctl enable --now server-monitor.service server-monitor-collect.timer server-monitor-analyze.timer \
  server-monitor-burst.timer
if [[ "${1:-}" == --public ]]; then systemctl enable --now server-monitor-cert-renew.timer; fi
systemctl list-timers 'server-monitor*' --no-pager
