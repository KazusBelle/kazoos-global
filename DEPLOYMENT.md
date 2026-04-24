# Deployment Runbook

## Deploy flow

1. Push changes to `main`.
2. GitHub Actions runs:
   - `CI` — Python tests, frontend typecheck/build, Docker build
   - `Deploy` — SSH deploy to VPS
3. `Deploy` workflow:
   - validates required secrets
   - sets up SSH key and `known_hosts`
   - ensures `VPS_DEPLOY_PATH` exists on VPS
   - `rsync`s repo to VPS
   - runs `docker compose build`
   - runs `docker compose up -d --remove-orphans`

## GitHub Actions secrets

- `VPS_HOST`: VPS IP or DNS name
- `VPS_USER`: SSH user for deploy
- `VPS_SSH_KEY`: private SSH key used by Actions
- `VPS_DEPLOY_PATH`: absolute project path on VPS, e.g. `/srv/kazus`

## Manual diagnostics

Local:

```bash
git status
git log --oneline -5
curl http://localhost:8000/healthz
docker compose ps
docker compose logs -f
PYTHONPATH=shared:backend:worker pytest -q
```

Server:

```bash
ssh user@host
cd /path/to/deploy
docker compose ps
docker compose logs -f backend worker frontend
docker compose logs --tail=200 db
curl http://127.0.0.1:8000/healthz
```

## Recovery commands

Rebuild and restart:

```bash
cd /path/to/deploy
docker compose build
docker compose up -d --remove-orphans
```

Restart a single service:

```bash
docker compose restart backend
docker compose restart worker
docker compose restart frontend
```

Pull logs for a failing service:

```bash
docker compose logs --tail=200 backend
docker compose logs --tail=200 worker
docker compose logs --tail=200 frontend
```

Hard refresh from current repo state on VPS:

```bash
rsync -az --delete --exclude='.git' --exclude='node_modules' --exclude='frontend/dist' --exclude='.env' --exclude='__pycache__' ./ user@host:/path/to/deploy/
ssh user@host 'cd /path/to/deploy && docker compose build && docker compose up -d --remove-orphans'
```
