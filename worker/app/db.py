from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kazus_db.models import Base  # noqa: F401 ensure metadata is registered

from .settings import get_settings

_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
