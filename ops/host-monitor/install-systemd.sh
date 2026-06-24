#!/usr/bin/env bash
# Install the kazus host-monitor as a systemd system timer (recommended).
# Requires root (systemd system units live in /etc/systemd/system).
#
#   sudo bash ops/host-monitor/install-systemd.sh
#
# Idempotent: re-running just refreshes the units + daemon-reload.
#
# By default it ONLY installs the unit files (no enable/start) so installation
# is side-effect free. Pass --enable (or ENABLE=1) to also enable+start the
# timer and remove the no-sudo cron bridge:
#   sudo bash ops/host-monitor/install-systemd.sh           # install files only
#   sudo bash ops/host-monitor/install-systemd.sh --enable  # install + start
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/etc/systemd/system
ENABLE="${ENABLE:-0}"
[[ "${1:-}" == "--enable" ]] && ENABLE=1

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0 [--enable]" >&2
  exit 1
fi

install -m 0644 "$HERE/kazus-monitor.service" "$DEST/kazus-monitor.service"
install -m 0644 "$HERE/kazus-monitor.timer"   "$DEST/kazus-monitor.timer"
systemctl daemon-reload
echo "Unit files installed + daemon-reload done."

if [[ "$ENABLE" == "1" ]]; then
  systemctl enable --now kazus-monitor.timer
  # Remove the cron bridge (if any) so the monitor doesn't run twice.
  if crontab -u deploy -l 2>/dev/null | grep -q "KAZUS-HOST-MONITOR"; then
    crontab -u deploy -l 2>/dev/null | grep -v "KAZUS-HOST-MONITOR" | crontab -u deploy -
    echo "Removed cron bridge for user deploy (systemd now owns the schedule)."
  fi
  echo "Timer enabled + started."
  systemctl list-timers kazus-monitor.timer --no-pager || true
else
  echo "Files installed only (not enabled). To start: sudo systemctl enable --now kazus-monitor.timer"
fi
echo "Smoke test one cycle: systemctl start kazus-monitor.service && journalctl -u kazus-monitor.service -n 8 --no-pager"
