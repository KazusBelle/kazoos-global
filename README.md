# Kazus Global — Crypto Screener (MVP)

A minimal screener dashboard for **Binance USDT-M Futures** built around the
logic from two Pine Script indicators (`kazus_global_v3.pine`,
`kazus_local_beta.pine`). For each manually-tracked symbol the stack
computes two snapshots — GLOBAL (D1) and LOCAL (H1) — classifies the
current price into a fibonacci zone (premium / equilibrium / discount /
OTE), and sends a Telegram alert on OTE entry.

## Stack

| Layer     | Tech |
|-----------|------|
| Backend   | Python 3.12 · FastAPI · SQLAlchemy 2 |
| Worker    | Python 3.12 · httpx · SQLAlchemy 2 |
| Database  | PostgreSQL 16 |
| Frontend  | Vite · React · TypeScript · Tailwind (dark screener UI) |
| Infra     | Docker Compose · Nginx (static+proxy) |
| CI/CD     | GitHub Actions (build+test on push, rsync/ssh deploy to VPS) |

## Repo layout

```
.
├─ backend/                  # FastAPI app (auth, coins CRUD, dashboard API)
│  └─ app/
├─ worker/                   # Recompute + Telegram alert loop
│  └─ app/
├─ shared/
│  ├─ kazus_logic/           # Pine→Python port (engine + binance client)
│  └─ kazus_db/              # SQLAlchemy models shared by backend & worker
├─ frontend/                 # Vite + React + Tailwind
├─ docker/                   # nginx.conf for the frontend container
├─ docs/pine/                # Source-of-truth Pine scripts (pointer files)
├─ tests/                    # pytest smoke tests
├─ .github/workflows/        # ci.yml + deploy.yml
├─ docker-compose.yml
└─ .env.example
```

## Quickstart — local

```bash
cp .env.example .env
# (optional for a quick try) leave defaults; for prod edit ADMIN_PASSWORD,
# JWT_SECRET, POSTGRES_PASSWORD, CORS_ORIGINS and TELEGRAM_* — see
# "Security" section.

docker compose up --build -d
```

Services exposed on the host:

| Service  | URL                       | Notes |
|----------|---------------------------|-------|
| Dashboard | <http://localhost:8080>  | nginx serves the SPA + reverse-proxies `/api` to the backend |
| Backend  | <http://localhost:8000>   | direct FastAPI (`/docs`, `/healthz`) — keep behind firewall in prod |

Default credentials (from `.env.example`):

| Field | Value |
|-------|-------|
| Username | `admin` |
| Password | `change-me` |

The worker only starts after the backend reports healthy (which means the
admin user and the default coin list have already been seeded). The first
compute cycle hits Binance immediately on startup and finishes in ~10 s
for the seeded 10 symbols. The frontend polls the dashboard every 15 s.

## Quickstart — VPS

1. Install Docker + Docker Compose plugin on the host.
2. On GitHub set repo secrets:
   - `VPS_HOST` – IP or DNS of the VPS
   - `VPS_USER` – SSH login
   - `VPS_SSH_KEY` – private key contents (PEM)
   - `VPS_DEPLOY_PATH` – absolute path on the VPS, e.g. `/srv/kazus`
3. First manual deploy:
   ```bash
   rsync -az --exclude .git --exclude node_modules \
     ./ user@host:/srv/kazus/
   ssh user@host 'cd /srv/kazus && cp .env.example .env && $EDITOR .env && docker compose up -d --build'
   ```
4. Subsequent deploys: push to `main` → GitHub Actions `Deploy` workflow
   rsyncs the repo and runs `docker compose up -d --build`.

## Pine source of truth

The files in `docs/pine/` are the canonical description of the signal
logic. The Python port lives in
[`shared/kazus_logic/engine.py`](shared/kazus_logic/engine.py) and is
fed bar-by-bar — no simplification, no smoothed approximation. If you
spot a divergence between Pine and Python, the Pine script wins.

### What each engine produces

Both engines produce the same `FibState` shape:

- `direction`: `"bullish" | "bearish" | "none"`
- `fib_low`, `fib_high`: anchor prices
- From these we derive `retracement` for the current price and classify:

```
premium    : retracement ∈ [0.0, 0.48)
equilibrium: retracement ∈ [0.48, 0.52]
discount   : retracement ∈ (0.52, 1.0]
OTE        : retracement ∈ [0.62, 0.79]   ← also discount, and sets Setup = "yes"
```

`setup = yes` **iff** the current price is inside OTE.

### Telegram alerts

The worker emits **one** Telegram notification when a coin transitions
from outside OTE to inside OTE (per timeframe). While the coin stays
inside OTE no further alerts are sent. When it leaves OTE the state
resets, and the next re-entry will alert again.

The timeframes that participate in alerts are controlled by
`ALERT_TIMEFRAMES` (default: `D1,H1`).

## Assumptions

The MVP codifies a few judgment calls where Pine left room:

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Zone bands — premium/equilibrium/discount split at **0.48 / 0.52** around 0.5, with 0.5-0.61 included in discount. | User instruction: _"трактовку 0.5–0.61 как discount"_. |
| 2 | OTE = **0.62–0.79**. | Matches user spec. |
| 3 | D1/H1 computations run on **closed bars** (the currently forming bar is dropped). | Pine's `request.security` fires on HTF close; we mirror that on ingestion. |
| 4 | Bar-by-bar — klines are fetched (`limit=500` for D1, `900` for H1) and replayed through the engine on every refresh. No persistent per-bar state. | Simpler to reason about; fast enough for a few hundred symbols. |
| 5 | `SystemStatus.last_refresh_at` is updated even when Binance errors out for some symbols; `last_error` surfaces the last failing symbol. | Users should see the screener is "alive" even with partial failures. |
| 6 | Telegram alert de-dup is per `(symbol, timeframe)` — stored in `alert_states`. | Matches "do not resend while inside OTE; resend after new entry" requirement. |
| 7 | Setup column is `yes/no` only (no partial / proximity tiers). | Mirrors the UI.png sketch. |
| 8 | The bootstrap admin (`ADMIN_USERNAME` / `ADMIN_PASSWORD`) is created only on first boot, when the users table is empty. | Avoids accidentally resetting prod credentials. |
| 9 | Manual coin additions are case-insensitive and normalized to upper-case. Duplicates are idempotent. | UX. |
| 10 | The `last_structure_event` surfaced as `trend` is a simplification of Pine's HH/HL/LL/LH. up = bull structure, down = bear structure, none = no confirmed structure yet. | Used purely for a decorative sparkline in the UI. |

If you discover a behavior mismatch with the Pine indicator, open an
issue against the exact bar index; the engine is deterministic and
reproducible.

## Running tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r worker/requirements.txt pytest
PYTHONPATH=shared:backend:worker pytest -q
```

## Security notes for MVP

- Bcrypt-hashed passwords; JWT (HS256) with configurable secret.
- **Change `JWT_SECRET` and `ADMIN_PASSWORD` before deploying.**
- CORS defaults to `*` for local dev — tighten via `CORS_ORIGINS`
  in production.
- No rate limiting; the UI and worker only poll the backend, but
  consider putting the deploy behind Cloudflare / Caddy for TLS.

## API cheat sheet

| Method | Path | Auth | Body |
|--------|------|------|------|
| POST   | `/api/auth/login`      | – | `application/x-www-form-urlencoded` (`username`, `password`) — used by Swagger `/docs` |
| POST   | `/api/auth/login-json` | – | `{username, password}` — used by the SPA |
| GET    | `/api/coins`           | Bearer | – |
| POST   | `/api/coins`           | Bearer | `{symbol}` (case-insensitive) |
| DELETE | `/api/coins/{symbol}`  | Bearer | – |
| GET    | `/api/dashboard`       | Bearer | – |
| GET    | `/healthz`             | – | – |

## Production checklist

Before exposing the stack publicly, change every value below in `.env`:

- `JWT_SECRET` — long random string (≥ 32 bytes). Defaults are guessable.
- `ADMIN_PASSWORD` — set **before** the first `docker compose up`. The
  admin row is seeded only when the `users` table is empty; later changes
  to `.env` do nothing. To rotate after the fact, update via SQL or add
  a new admin row.
- `POSTGRES_PASSWORD` — even if Postgres is only reachable inside the
  compose network.
- `CORS_ORIGINS` — replace `["*"]` with the actual frontend origin
  (e.g. `["https://kazus.example.com"]`).
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — otherwise OTE alerts
  only land in worker logs.
- `BACKEND_PORT` — drop it from the public host; the SPA only needs
  `:8080`. Either remove the `ports:` mapping for `backend` or bind it
  to `127.0.0.1:8000:8000`.
- `REFRESH_INTERVAL_SEC` — 300 s is sane for D1/H1; lower values + many
  symbols can hit Binance rate limits.
- Put a TLS terminator in front of port 8080 (Caddy / Cloudflare / nginx).
- Back up the `pgdata` Docker volume on a schedule.
