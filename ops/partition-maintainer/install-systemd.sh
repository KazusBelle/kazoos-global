#!/usr/bin/env bash
# Install the kazus liquidity_samples partition maintainer as a systemd system
# timer. Requires root (system units live in /etc/systemd/system).
#
#   sudo bash ops/partition-maintainer/install-systemd.sh           # install files only
#   sudo bash ops/partition-maintainer/install-systemd.sh --enable  # install + start
#
# Idempotent: re-running just refreshes the units + daemon-reload. By default it
# ONLY installs the unit files (no enable/start) so installation is side-effect
# free — pass --enable (or ENABLE=1) to also enable+start the daily timer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/etc/systemd/system
ENABLE="${ENABLE:-0}"
[[ "${1:-}" == "--enable" ]] && ENABLE=1

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0 [--enable]" >&2
  exit 1
fi

install -m 0644 "$HERE/kazus-partition-maintainer.service" "$DEST/kazus-partition-maintainer.service"
install -m 0644 "$HERE/kazus-partition-maintainer.timer"   "$DEST/kazus-partition-maintainer.timer"
systemctl daemon-reload
echo "Unit files installed + daemon-reload done."

if [[ "$ENABLE" == "1" ]]; then
  systemctl enable --now kazus-partition-maintainer.timer
  echo "Timer enabled + started."
  systemctl list-timers kazus-partition-maintainer.timer --no-pager || true
else
  echo "Files installed only (not enabled). To start:"
  echo "  sudo systemctl enable --now kazus-partition-maintainer.timer"
fi
echo "Dry-run one pass:  python3 $HERE/partition_maintainer.py --dry-run"
echo "Run one pass now:  sudo systemctl start kazus-partition-maintainer.service && journalctl -u kazus-partition-maintainer.service -n 20 --no-pager"
