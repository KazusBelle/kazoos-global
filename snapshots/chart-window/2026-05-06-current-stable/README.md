# Chart Window Snapshot: 2026-05-06 Current Stable

Frozen restore point for the current chart modal/window state before risky visual or logic changes.

This snapshot is intentionally not a live/private copy. It is a recovery checkpoint.
Do not edit files inside this folder by hand unless creating a new snapshot revision.

## What It Captures

- Current chart modal and dashboard UI implementation.
- Current chart CSS/layout.
- Current frontend API/error plumbing used by the chart window.
- Backend chart API behavior that affects displayed OTE/FVG/structure state.
- Shared Kazus logic files that influence setup/fib/structure decisions.
- Worker chart image file, because Telegram/chart output can diverge from the site.

## Base

- Base commit: `bd28930`
- Snapshot folder: `snapshots/chart-window/2026-05-06-current-stable`
- The working tree had uncommitted changes when this snapshot was created.
- Because of that, the real frozen state is the file copy under `files/`, not the git tag alone.

## Contents

- `files/` contains full copies of the restorable files, preserving repo-relative paths.
- `working-tree.diff` contains the tracked-file diff from base commit to this snapshot.
- `working-tree.stat` contains a short diff summary.
- `SHA256SUMS` records checksums for copied files.
- `manifest.json` records metadata and the file list.
- `restore.sh` restores the copied files into the repo root.

## Restore

From repo root:

```bash
bash snapshots/chart-window/2026-05-06-current-stable/restore.sh
npm --prefix frontend run build
docker compose up -d --build frontend backend worker
```

The restore script copies only the files included in this snapshot. It does not run git reset and does not delete unrelated files.

## Verify

```bash
cd snapshots/chart-window/2026-05-06-current-stable
sha256sum -c SHA256SUMS
```

