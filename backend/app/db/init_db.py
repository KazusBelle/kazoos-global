from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.security import hash_password
from ..models.models import User, Coin, SystemStatus
from .base import Base, engine, SessionLocal


# Minimal ad-hoc migration. The stack is still pre-production; switching to
# Alembic after the first real release is tracked as tech debt.
_ADDITIVE_MIGRATIONS = [
    # Postgres: `ADD COLUMN IF NOT EXISTS` is supported from PG 9.6+.
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS starred BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS pinned_order INTEGER",
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS call_tag VARCHAR(16)",
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS call_note TEXT",
    "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS closes_json TEXT",
    """
    CREATE TABLE IF NOT EXISTS alert_events (
        id SERIAL PRIMARY KEY,
        timeframe VARCHAR(8) NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_alert_events_timeframe ON alert_events (timeframe)",
    "CREATE INDEX IF NOT EXISTS ix_alert_events_created_at ON alert_events (created_at)",
    "ALTER TABLE alert_states ADD COLUMN IF NOT EXISTS in_setup BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE alert_states ADD COLUMN IF NOT EXISTS last_setup_alert_at TIMESTAMP",
    """
    CREATE TABLE IF NOT EXISTS user_tda_states (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL UNIQUE,
        coins_json TEXT,
        data_json TEXT,
        photos_json TEXT,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE user_tda_states ADD COLUMN IF NOT EXISTS photos_json TEXT",
    "CREATE INDEX IF NOT EXISTS ix_user_tda_states_user_id ON user_tda_states (user_id)",
]


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for stmt in _ADDITIVE_MIGRATIONS:
            conn.execute(text(stmt))


def seed_initial_data() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        _ensure_admin(db, settings.admin_username, settings.admin_password)
        _ensure_default_coins(db, settings.default_coins)
        _ensure_status_row(db)
        db.commit()


def _ensure_admin(db: Session, username: str, password: str) -> None:
    admin = db.query(User).filter(User.username == username).first()
    if admin is not None:
        admin.password_hash = hash_password(password)
        admin.is_admin = True
        return
    admin = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=True,
    )
    db.add(admin)


def _ensure_default_coins(db: Session, csv: str) -> None:
    if db.query(Coin).count() > 0:
        return
    for sym in [s.strip().upper() for s in csv.split(",") if s.strip()]:
        db.add(Coin(symbol=sym, is_active=True))


def _ensure_status_row(db: Session) -> None:
    if db.query(SystemStatus).filter(SystemStatus.id == 1).first() is None:
        db.add(SystemStatus(id=1))
