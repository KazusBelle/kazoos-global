import logging
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .api import auth, chart, coins, dashboard, frontend_logs, liquidity, system, tda
from .core.config import get_settings
from .db.base import engine
from .db.init_db import create_schema, seed_initial_data
from .services.server_metrics import run_server_metrics_collector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("kazus.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. Replaces @app.on_event, deprecated since FastAPI 0.93.

    Startup failures are fatal on purpose. The previous version caught every
    exception, logged it and carried on, so a backend that never got its schema
    still served traffic — and still reported healthy — while answering every
    request with an error. With `restart: unless-stopped` crashing is the
    useful behaviour: the container retries instead of pretending.
    """
    create_schema()
    seed_initial_data()
    stop_event = asyncio.Event()
    app.state.server_metrics_stop = stop_event
    app.state.server_metrics_task = asyncio.create_task(
        run_server_metrics_collector(stop_event)
    )
    app.state.ready = True
    logger.info("startup complete — schema ready, serving")
    try:
        yield
    finally:
        app.state.ready = False
        stop_event.set()
        task = getattr(app.state, "server_metrics_task", None)
        if task is not None:
            await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.ready = False

    # allow_credentials is deliberately off. Combined with a "*" origin it is
    # forbidden by the CORS spec, and Starlette works around that by echoing
    # back whatever Origin the request carried — which grants every site the
    # access the wildcard appeared to grant anonymously. Auth here travels in
    # the Authorization header, not cookies, so nothing needs credentialed
    # CORS. Turning it back on would also require naming explicit origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(coins.router, prefix=settings.api_prefix)
    app.include_router(dashboard.router, prefix=settings.api_prefix)
    app.include_router(chart.router, prefix=settings.api_prefix)
    app.include_router(frontend_logs.router, prefix=settings.api_prefix)
    app.include_router(tda.router, prefix=settings.api_prefix)
    app.include_router(liquidity.router, prefix=settings.api_prefix)
    app.include_router(system.router, prefix=settings.api_prefix)

    # Observation-only timing logger for the LIQ initial-load audit.
    # Logs elapsed_ms for /api/liquidity/* requests only — no header
    # injection, no response mutation, no UI exposure. Used to gather
    # cold/warm timings before any optimization is attempted.
    liq_prefix = f"{settings.api_prefix}/liquidity"

    @app.middleware("http")
    async def _liquidity_timing(request: Request, call_next):
        path = request.url.path
        if not path.startswith(liq_prefix):
            return await call_next(request)
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # For /snapshot we also capture the symbols-count from the query
        # string — that's the dimension we expect timing to scale with.
        sym_count = ""
        if path.endswith("/snapshot") or path.endswith("/snapshot/replay"):
            symbols_raw = request.query_params.get("symbols", "")
            sym_count = f" symbols={len(symbols_raw.split(',')) if symbols_raw else 0}"
        logger.info(
            "liq_timing path=%s status=%d elapsed_ms=%d%s",
            path, response.status_code, elapsed_ms, sym_count,
        )
        return response

    @app.get("/healthz")
    def healthz(response: Response):
        """Prove the process can actually serve, don't just confirm it is alive.

        Three systems act on this answer: the Docker healthcheck, the host
        monitor, and the worker's `depends_on: condition: service_healthy`
        gate. It used to return a constant, so all three read "healthy" right
        through outages — which is how an eight-day collection gap and a
        backend answering errors both went unnoticed. A liveness probe that
        cannot fail is not a probe.

        Kept cheap: one SELECT 1 on a pooled connection, called every 30s.
        If the database is gone this blocks on pool_timeout and Docker's own
        5s healthcheck timeout marks the container unhealthy — also correct.
        """
        if not getattr(app.state, "ready", False):
            response.status_code = 503
            return {"ok": False, "reason": "starting"}
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("healthz: database unreachable: %s", exc)
            response.status_code = 503
            return {"ok": False, "reason": "db"}
        return {"ok": True}

    return app


app = create_app()
