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

## Runtime env on VPS

The deploy workflow syncs code to the server, but runtime configuration
still comes from the server-side `.env` file in `VPS_DEPLOY_PATH`.

Most important values:

- `JWT_SECRET`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `CORS_ORIGINS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `REFRESH_INTERVAL_SEC`

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

Quick post-deploy checks:

```bash
docker compose ps
docker compose logs --tail=100 backend
docker compose logs --tail=100 worker
curl http://127.0.0.1:8000/healthz
curl -I http://127.0.0.1/
```

## Production checklist

- Set strong values for `JWT_SECRET`, `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`
- Keep backend off the public internet unless intentionally exposed
- Set strict `CORS_ORIGINS` instead of `["*"]`
- Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` if alerts are required
- Ensure `.env` exists on the VPS before first prod deploy
- Ensure Docker and Docker Compose plugin are installed on VPS
- Ensure `VPS_DEPLOY_PATH` has enough disk space for images and volumes
- Put TLS/HTTPS in front of the frontend endpoint
- Back up the Postgres volume regularly
- Monitor `docker compose logs` and container restart loops after deploys
- Keep SSH deploy key scoped to deploy only
- Verify `docker compose ps` and `/healthz` after each production deploy

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

Full reset of containers without deleting data volumes:

```bash
cd /path/to/deploy
docker compose down
docker compose up -d --build
```
