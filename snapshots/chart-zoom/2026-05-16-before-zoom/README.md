Snapshot before the Telegram chart-export X-zoom change.

Captures the pre-zoom (git HEAD) state of every file touched by the zoom
feature. Run `restore.sh` to roll back if the zoomed render misbehaves.

Files: CandleChart.tsx, ChartExportPage.tsx, chart_renderer.py,
settings.py, telegram_alerts.py, docker-compose.yml.

Note: the zoom is also a clean git commit, so `git revert` is the
preferred rollback. This snapshot is the belt-and-suspenders copy.
