import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import auth, coins, dashboard
from .core.config import get_settings
from .db.init_db import create_schema, seed_initial_data

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

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.on_event("startup")
    def on_startup():
        try:
            create_schema()
            seed_initial_data()
        except Exception as exc:
            logger.exception("startup initialization failed: %s", exc)

    return app


app = create_app()
