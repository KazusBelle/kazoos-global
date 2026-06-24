#!/usr/bin/env bash
# No-sudo bridge: schedule the host-monitor via the current user's crontab.
# Runs every minute and once at boot. Survives reboot (cron is a boot service)
# and is independent of Docker. Use this when you cannot install the systemd
# units (no root). Promote to systemd later with install-systemd.sh, which
# auto-removes this cron entry.
#
#   bash ops/host-monitor/install-cron.sh
set -euo pipefail

SCRIPT=/home/deploy/workspace/kazus-global/ops/host-monitor/monitor.py
LOG=/home/deploy/.kazus-monitor.log
MARK="KAZUS-HOST-MONITOR"

# Drop any previous entry, then append fresh ones (idempotent).
( crontab -l 2>/dev/null | grep -v "$MARK" || true
  echo "* * * * * /usr/bin/python3 $SCRIPT >> $LOG 2>&1  # $MARK"
  echo "@reboot sleep 60 && /usr/bin/python3 $SCRIPT >> $LOG 2>&1  # $MARK"
) | crontab -

echo "Cron bridge installed for $(whoami):"
crontab -l | grep "$MARK"
echo "Log: $LOG"
