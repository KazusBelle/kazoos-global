from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kazus_db.models import Base  # re-exported

from ..core.config import get_settings


_settings = get_settings()
# Pool sized for what actually runs. The previous 20+20 was chosen for "~10
# simultaneous Coordination/Discovery tabs each holding a session through an
# uncached heavy call" — those pages and their endpoints are gone. Measured
# steady state is 3 checked-out connections, so 5+10 keeps a fivefold margin.
#   pool_timeout=10   fail fast instead of stacking client waits
#   pool_recycle=1800 reset connections every 30 min to dodge stale TCP
#   pool_pre_ping     verify the connection is alive before checkout
#
# statement_timeout is set per CONNECTION, not per request. It used to be a
# `SET LOCAL` inside get_db(), which binds to the open transaction only — so
# in the seven handlers that commit mid-request every query afterwards ran
# with no ceiling at all, exactly where a runaway is most likely.
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=10,
    pool_recycle=1800,
    connect_args={"options": "-c statement_timeout=45s"},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def pool_status() -> dict:
    """Snapshot of the pool — exposed via /admin/runtime-health for ops
    visibility. ``checked_out`` ≈ active concurrent users; sustained high
    values are the early-warning sign of pool exhaustion."""
    pool = engine.pool
    return {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
    }


def get_db():
    # statement_timeout now comes from connect_args above, so it holds for the
    # whole connection and survives commits inside a request.
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["Base", "engine", "SessionLocal", "get_db"]
