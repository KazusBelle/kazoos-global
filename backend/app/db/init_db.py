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
    "ALTER TABLE alert_states ADD COLUMN IF NOT EXISTS last_setup_alert_at TIMESTAMP",
    "ALTER TABLE alert_states ADD COLUMN IF NOT EXISTS sent_event_ids TEXT",
    "ALTER TABLE alert_states ADD COLUMN IF NOT EXISTS setup_state_json TEXT",
    "ALTER TABLE alert_states DROP COLUMN IF EXISTS last_setup_kind",
    "ALTER TABLE alert_states DROP COLUMN IF EXISTS in_setup",
    # Snapshot.setup widened from yes/no flag to setup-kind label.
    "ALTER TABLE snapshots ALTER COLUMN setup TYPE VARCHAR(16)",
    "ALTER TABLE snapshots ALTER COLUMN setup SET DEFAULT ''",
    "UPDATE snapshots SET setup = '' WHERE setup IN ('no', 'yes')",
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
    # liquidity_pins: persistent LIQ-scanner pins; worker reads this set
    # to drive WS subscriptions in addition to liquidity_active_subs.
    """
    CREATE TABLE IF NOT EXISTS liquidity_pins (
        id SERIAL PRIMARY KEY,
        symbol VARCHAR(32) NOT NULL,
        pinned_order INTEGER NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_liquidity_pins_symbol UNIQUE (symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liquidity_pins_symbol ON liquidity_pins (symbol)",
    # liquidity_ws_status: single-row health table written by the worker.
    """
    CREATE TABLE IF NOT EXISTS liquidity_ws_status (
        id INTEGER PRIMARY KEY,
        conn_id INTEGER NOT NULL DEFAULT 0,
        connected BOOLEAN NOT NULL DEFAULT FALSE,
        subscribed_json TEXT,
        last_message_at TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Phase-7 research persistence: alert log + annotations + crossex history.
    """
    CREATE TABLE IF NOT EXISTS liquidity_alert_history (
        id SERIAL PRIMARY KEY,
        alert_id VARCHAR(96) NOT NULL,
        symbol VARCHAR(32) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        severity VARCHAR(16) NOT NULL,
        regime VARCHAR(32) NOT NULL,
        confidence DOUBLE PRECISION NOT NULL,
        priority DOUBLE PRECISION NOT NULL,
        trigger TEXT NOT NULL DEFAULT '',
        started_at_ms BIGINT NOT NULL,
        last_seen_at_ms BIGINT NOT NULL,
        validated_outcome VARCHAR(24),
        validated_at_ms BIGINT,
        validation_notes TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_liq_alert_history_alert_id UNIQUE (alert_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_alert_history_symbol_ts ON liquidity_alert_history (symbol, started_at_ms)",
    "CREATE INDEX IF NOT EXISTS ix_liq_alert_history_kind_ts ON liquidity_alert_history (kind, started_at_ms)",
    """
    CREATE TABLE IF NOT EXISTS liquidity_annotations (
        id SERIAL PRIMARY KEY,
        symbol VARCHAR(32) NOT NULL,
        ts_ms BIGINT NOT NULL,
        kind VARCHAR(32) NOT NULL,
        note TEXT,
        user_id INTEGER,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_annotations_symbol_ts ON liquidity_annotations (symbol, ts_ms)",
    """
    CREATE TABLE IF NOT EXISTS liquidity_crossex_history (
        id SERIAL PRIMARY KEY,
        symbol VARCHAR(32) NOT NULL,
        exchange VARCHAR(16) NOT NULL,
        ts_ms BIGINT NOT NULL,
        funding_rate DOUBLE PRECISION,
        open_interest_usd DOUBLE PRECISION,
        spread_fraction DOUBLE PRECISION,
        mid_price DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_crossex_history_sym_ts ON liquidity_crossex_history (symbol, ts_ms)",
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
