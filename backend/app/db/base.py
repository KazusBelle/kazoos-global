from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kazus_db.models import Base  # re-exported

from ..core.config import get_settings


_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["Base", "engine", "SessionLocal", "get_db"]
