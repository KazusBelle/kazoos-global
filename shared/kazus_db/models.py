from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, UniqueConstraint
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
    setup: Mapped[str] = mapped_column(String(4), default="no")
    retracement: Mapped[Optional[float]] = mapped_column(Float)

    fib_low: Mapped[Optional[float]] = mapped_column(Float)
    fib_high: Mapped[Optional[float]] = mapped_column(Float)
    ote_low_price: Mapped[Optional[float]] = mapped_column(Float)
    ote_high_price: Mapped[Optional[float]] = mapped_column(Float)

    trend: Mapped[str] = mapped_column(String(8), default="none")

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
    last_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SystemStatus(Base):
    __tablename__ = "system_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(String(512))
