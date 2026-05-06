#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SNAPSHOT_DIR/../../.." && pwd)"

cd "$REPO_ROOT"

cp "$SNAPSHOT_DIR/files/backend/app/api/chart.py" "backend/app/api/chart.py"
cp "$SNAPSHOT_DIR/files/backend/app/api/frontend_logs.py" "backend/app/api/frontend_logs.py"
cp "$SNAPSHOT_DIR/files/backend/app/main.py" "backend/app/main.py"
cp "$SNAPSHOT_DIR/files/frontend/index.html" "frontend/index.html"
cp "$SNAPSHOT_DIR/files/frontend/src/components/Dashboard.tsx" "frontend/src/components/Dashboard.tsx"
cp "$SNAPSHOT_DIR/files/frontend/src/index.css" "frontend/src/index.css"
cp "$SNAPSHOT_DIR/files/frontend/src/lib/api.ts" "frontend/src/lib/api.ts"
cp "$SNAPSHOT_DIR/files/frontend/src/lib/frontendErrors.tsx" "frontend/src/lib/frontendErrors.tsx"
cp "$SNAPSHOT_DIR/files/frontend/src/main.tsx" "frontend/src/main.tsx"
cp "$SNAPSHOT_DIR/files/shared/kazus_logic/compute.py" "shared/kazus_logic/compute.py"
cp "$SNAPSHOT_DIR/files/shared/kazus_logic/engine.py" "shared/kazus_logic/engine.py"
cp "$SNAPSHOT_DIR/files/worker/app/chart_image.py" "worker/app/chart_image.py"

echo "Restored chart window snapshot: 2026-05-06-current-stable"
