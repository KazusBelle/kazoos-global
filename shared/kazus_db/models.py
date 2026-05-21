from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Coin(Base):
    __tablename__ = "coins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Legacy — kept for schema stability; new UI uses pinned_order instead.
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # NULL = not pinned. 0..N = pinned in this sort order (smaller on top).
    pinned_order: Mapped[Optional[int]] = mapped_column(Integer)
    # CALL marker — one of: vanga, voldemar, makiavelli, me (or None)
    call_tag: Mapped[Optional[str]] = mapped_column(String(16))
    call_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", name="uq_snapshot_sym_tf"),
        Index("ix_snapshot_sym_tf", "symbol", "timeframe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))

    price: Mapped[Optional[float]] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(16), default="none")
    zone: Mapped[str] = mapped_column(String(16), default="none")
    in_ote: Mapped[bool] = mapped_column(Boolean, default=False)
    # "" (no setup) | "Inversion" | "Created"
    setup: Mapped[str] = mapped_column(String(16), default="")
    retracement: Mapped[Optional[float]] = mapped_column(Float)

    fib_low: Mapped[Optional[float]] = mapped_column(Float)
    fib_high: Mapped[Optional[float]] = mapped_column(Float)
    ote_low_price: Mapped[Optional[float]] = mapped_column(Float)
    ote_high_price: Mapped[Optional[float]] = mapped_column(Float)

    trend: Mapped[str] = mapped_column(String(8), default="none")

    # JSON-encoded list of last N close prices (for sparkline). Text so
    # it works cross-DB without JSONB.
    closes_json: Mapped[Optional[str]] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AlertState(Base):
    __tablename__ = "alert_states"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", name="uq_alert_sym_tf"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    in_ote: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON array of setup event_ids already alerted within the current OTE
    # window. Cleared when price exits OTE so re-entry restarts the stream.
    sent_event_ids: Mapped[Optional[str]] = mapped_column(Text)
    # JSON-serialized SetupState carried across worker cycles (long-only
    # INV/CRE/STB state machine). Null until the first detector run.
    setup_state_json: Mapped[Optional[str]] = mapped_column(Text)
    last_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_setup_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SystemStatus(Base):
    __tablename__ = "system_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(String(512))


class LiquiditySample(Base):
    """Time-series sample for a single liquidity metric on a single symbol.

    Worker writes one row per (symbol, metric) per polling cycle. `ts` is
    epoch-ms of the sample. `price` is the mark / close price at sample
    time so charts can plot price alongside the metric without a second
    join. Each metric is its own row — never merged into a wide table —
    so adding new metrics doesn't require a migration.
    """

    __tablename__ = "liquidity_samples"
    __table_args__ = (
        Index(
            "ix_liq_samples_symbol_metric_ts",
            "symbol",
            "metric",
            "ts",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Float)
    price: Mapped[Optional[float]] = mapped_column(Float)


class UserTDAState(Base):
    __tablename__ = "user_tda_states"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_tda_states_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    coins_json: Mapped[Optional[str]] = mapped_column(Text)
    data_json: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
