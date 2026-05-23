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
    # Phase-11 anomaly memory.
    """
    CREATE TABLE IF NOT EXISTS liquidity_anomaly_memory (
        id SERIAL PRIMARY KEY,
        kind VARCHAR(48) NOT NULL,
        severity VARCHAR(16) NOT NULL DEFAULT 'info',
        occurred_at_ms BIGINT NOT NULL,
        fingerprint_json TEXT NOT NULL,
        novelty_score DOUBLE PRECISION NOT NULL DEFAULT 100.0,
        recurrence_count INTEGER NOT NULL DEFAULT 0,
        related_alert_ids_json TEXT,
        notes TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_anomaly_memory_occurred_ts ON liquidity_anomaly_memory (occurred_at_ms)",
    "CREATE INDEX IF NOT EXISTS ix_liq_anomaly_memory_kind ON liquidity_anomaly_memory (kind)",
    # Phase-13 anomaly genealogy + intelligence evolution timeline.
    """
    CREATE TABLE IF NOT EXISTS liquidity_anomaly_edges (
        id SERIAL PRIMARY KEY,
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        kind VARCHAR(32) NOT NULL,
        weight DOUBLE PRECISION DEFAULT 1.0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_liq_anom_edge_triple UNIQUE (from_id, to_id, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_anom_edge_from ON liquidity_anomaly_edges (from_id)",
    "CREATE INDEX IF NOT EXISTS ix_liq_anom_edge_to ON liquidity_anomaly_edges (to_id)",
    """
    CREATE TABLE IF NOT EXISTS liquidity_intelligence_history (
        id SERIAL PRIMARY KEY,
        ts_ms BIGINT NOT NULL,
        synthesized_stress DOUBLE PRECISION,
        coordinated_state VARCHAR(48),
        cross_layer_agreement DOUBLE PRECISION,
        structural_break_score DOUBLE PRECISION,
        meta_confidence_score DOUBLE PRECISION,
        meta_intelligence_health DOUBLE PRECISION,
        health_state VARCHAR(24),
        risk_state_score DOUBLE PRECISION,
        regime_shift_probability DOUBLE PRECISION,
        dominant_regime VARCHAR(32),
        fingerprint_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_liq_intel_history_ts ON liquidity_intelligence_history (ts_ms)",
    """
    CREATE TABLE IF NOT EXISTS server_metrics (
        id SERIAL PRIMARY KEY,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        load_1m DOUBLE PRECISION,
        load_5m DOUBLE PRECISION,
        load_15m DOUBLE PRECISION,
        cpu_percent DOUBLE PRECISION,
        memory_percent DOUBLE PRECISION,
        swap_percent DOUBLE PRECISION,
        disk_percent DOUBLE PRECISION,
        net_connections INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_server_metrics_created_at ON server_metrics (created_at)",
    # Phase-17 Pass B: operator persistence tables.
    """
    CREATE TABLE IF NOT EXISTS operator_priority_history (
        id SERIAL PRIMARY KEY,
        priority_key VARCHAR(192) NOT NULL,
        source_layer VARCHAR(24) NOT NULL,
        kind VARCHAR(64) NOT NULL,
        headline TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        rationale TEXT NOT NULL DEFAULT '',
        priority_score DOUBLE PRECISION NOT NULL,
        current_escalation VARCHAR(12) NOT NULL,
        severity_raw DOUBLE PRECISION NOT NULL DEFAULT 0,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        source_weight DOUBLE PRECISION NOT NULL DEFAULT 1,
        current_lifecycle VARCHAR(16) NOT NULL DEFAULT 'NEW',
        current_status VARCHAR(12) NOT NULL DEFAULT 'active',
        first_seen_at_ms BIGINT NOT NULL,
        last_seen_at_ms BIGINT NOT NULL,
        resolved_at_ms BIGINT,
        occurrence_count INTEGER NOT NULL DEFAULT 1,
        peak_priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
        peak_escalation VARCHAR(12) NOT NULL DEFAULT 'NORMAL',
        members_json TEXT,
        extra_json TEXT,
        CONSTRAINT uq_operator_priority_history_key UNIQUE (priority_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_operator_priority_history_last_seen ON operator_priority_history (last_seen_at_ms)",
    "CREATE INDEX IF NOT EXISTS ix_operator_priority_history_status_escalation ON operator_priority_history (current_status, current_escalation)",
    """
    CREATE TABLE IF NOT EXISTS operator_priority_events (
        id SERIAL PRIMARY KEY,
        ts_ms BIGINT NOT NULL,
        priority_key VARCHAR(192) NOT NULL,
        source_layer VARCHAR(24) NOT NULL,
        event_type VARCHAR(24) NOT NULL,
        priority_before DOUBLE PRECISION,
        priority_after DOUBLE PRECISION,
        escalation_before VARCHAR(12),
        escalation_after VARCHAR(12),
        note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_operator_priority_events_key_ts ON operator_priority_events (priority_key, ts_ms)",
    "CREATE INDEX IF NOT EXISTS ix_operator_priority_events_ts_type ON operator_priority_events (ts_ms, event_type)",
    """
    CREATE TABLE IF NOT EXISTS operator_acknowledgements (
        id SERIAL PRIMARY KEY,
        priority_key VARCHAR(192) NOT NULL,
        action VARCHAR(16) NOT NULL,
        created_at_ms BIGINT NOT NULL,
        expires_at_ms BIGINT,
        user_id INTEGER,
        note TEXT,
        active BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_operator_ack_key_active ON operator_acknowledgements (priority_key, active)",
    "CREATE INDEX IF NOT EXISTS ix_operator_ack_created ON operator_acknowledgements (created_at_ms)",
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
