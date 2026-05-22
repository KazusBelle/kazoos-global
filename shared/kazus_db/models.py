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


class LiquidityActiveSub(Base):
    """A request that a symbol be live-tracked via WS for liquidity
    microstructure metrics. Written by the backend when a modal opens
    (and refreshed every ~30s via heartbeat). The worker reconciles the
    subscription set against this table every few seconds; rows where
    `expires_at < now()` are treated as no longer active and the worker
    unsubscribes from the corresponding Binance streams.

    One row per symbol — uniqueness is on the symbol so the heartbeat is
    an UPSERT that just bumps `expires_at` forward.
    """

    __tablename__ = "liquidity_active_subs"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_liquidity_active_subs_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class LiquidityPin(Base):
    """A symbol pinned to the top of the LIQ scanner — pin survives reload,
    is visible to the worker, and triggers a permanent WS subscription
    (subject to a cap). Independent of the TDA `coins.pinned_order` set.

    One row per symbol; `pinned_order` is dense 0..N-1 with smaller =
    higher in the table. Capacity is enforced at the API layer (≤20).
    """

    __tablename__ = "liquidity_pins"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_liquidity_pins_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    pinned_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiquidityWsStatus(Base):
    """Single-row table — worker writes connection health here every
    reconcile so the frontend can show live / stale / reconnect badges.

    `subscribed_json` is a JSON array of currently-subscribed symbols;
    `last_message_at` is the wall-clock of the most recent frame on any
    stream — stale detection compares against now().
    """

    __tablename__ = "liquidity_ws_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conn_id: Mapped[int] = mapped_column(Integer, default=0)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    subscribed_json: Mapped[Optional[str]] = mapped_column(Text)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class LiquidityAlertHistory(Base):
    """Persistent record of every alert the client-side engine promotes.

    The Phase-5 engine de-dupes and debounces in the browser, so by the
    time a row lands here it has already survived the persistence window
    (≥8s of continuous proposing). We store the promoted state plus a
    space for `validated_outcome` — filled later by a PATCH once the
    post-event window has elapsed, so research stats can compute
    follow-through and precision per kind.

    Why client-driven persistence? The regime/intelligence rules live in
    TS; replicating them in Python would double the surface area and
    introduce drift. Posting from the frontend keeps the rule set
    single-sourced.
    """

    __tablename__ = "liquidity_alert_history"
    __table_args__ = (
        UniqueConstraint("alert_id", name="uq_liq_alert_history_alert_id"),
        Index("ix_liq_alert_history_symbol_ts", "symbol", "started_at_ms"),
        Index("ix_liq_alert_history_kind_ts", "kind", "started_at_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(96), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Populated by the PATCH call once the validation window elapses:
    # "followed_through" | "noise" | "pending" | NULL (never validated).
    validated_outcome: Mapped[Optional[str]] = mapped_column(String(24))
    validated_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    validation_notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiquidityAnnotation(Base):
    """Replay annotations — user tags a (symbol, ts) with a label.

    Powers the research dataset: every annotation is a labelled training
    point we can use to look back at what made the user mark the moment.
    Free-form notes + a small set of structured kinds; `kind` is what
    queries aggregate on.
    """

    __tablename__ = "liquidity_annotations"
    __table_args__ = (
        Index("ix_liq_annotations_symbol_ts", "symbol", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # "useful_signal" | "false_signal" | "manipulation" | "interesting_setup"
    # | "liquidation_event" | "spoof_behavior" | "other"
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiquidityCrossExHistory(Base):
    """Persistent log of cross-exchange snapshots so venue-quality stats
    (divergence frequency, leadership) can aggregate over time. Writes
    happen whenever the API serves a `/crossex/{symbol}` request — that
    keeps the table sparse and self-driving without a dedicated worker
    loop.
    """

    __tablename__ = "liquidity_crossex_history"
    __table_args__ = (
        Index("ix_liq_crossex_history_sym_ts", "symbol", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    funding_rate: Mapped[Optional[float]] = mapped_column(Float)
    open_interest_usd: Mapped[Optional[float]] = mapped_column(Float)
    spread_fraction: Mapped[Optional[float]] = mapped_column(Float)
    mid_price: Mapped[Optional[float]] = mapped_column(Float)


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
