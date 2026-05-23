from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kazus_db.models import Base  # re-exported

from ..core.config import get_settings


_settings = get_settings()
# Explicit pool config — defaults (5 + 10 overflow, no timeout) were sized
# for snappy CRUD and starve under heavy research endpoints. Sized for the
# audit's worst case: ~10 simultaneous Coordination/Discovery tabs each
# holding one session for the duration of an uncached heavy call.
#   pool_size=20      base connections held open
#   max_overflow=20   burst capacity above pool_size
#   pool_timeout=10   fail fast (10s) instead of stacking client waits
#   pool_recycle=1800 reset connections every 30 min to dodge stale TCP
#   pool_pre_ping     verify the connection is alive before checkout
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    pool_timeout=10,
    pool_recycle=1800,
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
    db = SessionLocal()
    try:
        # Hard ceiling per request so a pathological query (e.g. a future
        # propagation reading 1M+ alerts after a cascade) can't hold the
        # pool connection forever. 45s leaves headroom above the slowest
        # uncached call observed (synthesis ≈ 30s) but kills runaways.
        # SET LOCAL scopes to this session only; closing returns it to
        # the pool with global defaults restored.
        from sqlalchemy import text as _text
        db.execute(_text("SET LOCAL statement_timeout = '45s'"))
        yield db
    finally:
        db.close()


__all__ = ["Base", "engine", "SessionLocal", "get_db"]
