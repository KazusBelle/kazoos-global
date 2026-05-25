import logging
import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api import auth, chart, coins, dashboard, frontend_logs, liquidity, system, tda
from .core.config import get_settings
from .db.init_db import create_schema, seed_initial_data
from .services.server_metrics import run_server_metrics_collector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("kazus.backend")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
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
    def healthz():
        return {"ok": True}

    @app.on_event("startup")
    async def on_startup():
        try:
            create_schema()
            seed_initial_data()
            stop_event = asyncio.Event()
            app.state.server_metrics_stop = stop_event
            app.state.server_metrics_task = asyncio.create_task(
                run_server_metrics_collector(stop_event)
            )
        except Exception as exc:
            logger.exception("startup initialization failed: %s", exc)

    @app.on_event("shutdown")
    async def on_shutdown():
        stop_event = getattr(app.state, "server_metrics_stop", None)
        task = getattr(app.state, "server_metrics_task", None)
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            await task

    return app


app = create_app()
