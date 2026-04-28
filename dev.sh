#!/usr/bin/env bash
# dev.sh — управление Vite dev server (HMR) vs nginx prod
set -euo pipefail

CMD=${1:-status}

case "$CMD" in
  up|start)
    echo "→ Запуск DEV режима (Vite HMR на :8080)..."
    # Остановить Docker frontend если запущен
    docker compose stop frontend 2>/dev/null || true
    docker rm kazus-global-frontend-1 2>/dev/null || true
    # Запустить Vite через pm2
    cd "$(dirname "$0")/frontend"
    pm2 delete kazus-frontend 2>/dev/null || true
    pm2 start npm --name kazus-frontend -- run dev -- --port 8080
    pm2 save
    echo "✓ Dev server запущен: http://193.68.67.122:8080"
    echo "  Логи: pm2 logs kazus-frontend"
    ;;
  down|prod)
    echo "→ Переключение на PROD (nginx)..."
    pm2 delete kazus-frontend 2>/dev/null || true
    pm2 save
    cd "$(dirname "$0")"
    docker compose up -d frontend
    echo "✓ Nginx frontend запущен: http://193.68.67.122:8080"
    ;;
  logs)
    pm2 logs kazus-frontend
    ;;
  status)
    echo "=== pm2 ==="
    pm2 list
    echo "=== Docker frontend ==="
    docker compose ps frontend
    ;;
  *)
    echo "Usage: $0 [up|down|logs|status]"
    exit 1
    ;;
esac
