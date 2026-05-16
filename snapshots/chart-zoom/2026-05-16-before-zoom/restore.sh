#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SNAPSHOT_DIR/../../.." && pwd)"

cd "$REPO_DIR"
for f in \
  frontend/src/components/CandleChart.tsx \
  frontend/src/components/ChartExportPage.tsx \
  worker/app/chart_renderer.py \
  worker/app/settings.py \
  worker/app/telegram_alerts.py \
  docker-compose.yml; do
  cp "$SNAPSHOT_DIR/files/$f" "$f"
  echo "Restored $f"
done
echo "Restored pre-zoom state from snapshots/chart-zoom/2026-05-16-before-zoom"
