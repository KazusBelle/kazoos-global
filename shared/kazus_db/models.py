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


class ServerMetric(Base):
    __tablename__ = "server_metrics"
    __table_args__ = (
        Index("ix_server_metrics_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    load_1m: Mapped[Optional[float]] = mapped_column(Float)
    load_5m: Mapped[Optional[float]] = mapped_column(Float)
    load_15m: Mapped[Optional[float]] = mapped_column(Float)
    cpu_percent: Mapped[Optional[float]] = mapped_column(Float)
    memory_percent: Mapped[Optional[float]] = mapped_column(Float)
    swap_percent: Mapped[Optional[float]] = mapped_column(Float)
    disk_percent: Mapped[Optional[float]] = mapped_column(Float)
    net_connections: Mapped[Optional[int]] = mapped_column(Integer)


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
        # (symbol, metric, ts) — for symbol-scoped reads (chart, replay).
        Index(
            "ix_liq_samples_symbol_metric_ts",
            "symbol",
            "metric",
            "ts",
        ),
        # (metric, ts) — for cross-symbol aggregations (pattern_discovery,
        # any "all symbols at metric X over time range" reads). Without
        # this, the planner falls back to a parallel seq scan on the 1M+
        # rows/day samples table and spills to disk on sort.
        Index(
            "ix_liq_samples_metric_ts",
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


class LiquidityAnomalyMemory(Base):
    """Persistent record of structural anomalies the engine has noticed.

    Phase-11 self-calibration needs long-term memory: "have we seen this
    kind of structural break / regime collapse / venue divergence before,
    or is this new?". Each row is one observation; recurrence is detected
    later by similarity over `fingerprint_json` rather than by primary
    key — anomaly kinds aren't strictly typed, so we cluster by content.

    The novelty_score is set at insertion time (0 = exact recurrence,
    100 = never seen anything like it) and is what makes the memory
    layer worth keeping — it gives the UI an at-a-glance sense of
    whether to take this seriously or treat it as a known pattern.
    """

    __tablename__ = "liquidity_anomaly_memory"
    __table_args__ = (
        Index("ix_liq_anomaly_memory_occurred_ts", "occurred_at_ms"),
        Index("ix_liq_anomaly_memory_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # "structural_break" | "regime_collapse" | "venue_divergence" |
    # "pre_cascade" | "edge_inversion" | "regime_emergence"
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # JSON-encoded compact state fingerprint (metric_name -> value) that
    # can be diffed against future anomalies for recurrence detection.
    fingerprint_json: Mapped[str] = mapped_column(Text, nullable=False)
    novelty_score: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    recurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    related_alert_ids_json: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiquidityAnomalyEdge(Base):
    """Directed edge between two `liquidity_anomaly_memory` rows.

    Phase-13 builds market-behavior genealogy by linking anomalies in
    causal / similarity / escalation relationships. Edges are typed —
    "caused_by" means the parent contributed to the child arising,
    "historically_similar" is non-directional in meaning but stored
    with from_id < to_id by convention to dedup, "evolved_into" is the
    escalation tree path, etc.

    Edges are auto-created by the worker's anomaly recorder when a new
    row lands; they're never deleted automatically — the graph is the
    memory.
    """

    __tablename__ = "liquidity_anomaly_edges"
    __table_args__ = (
        UniqueConstraint("from_id", "to_id", "kind", name="uq_liq_anom_edge_triple"),
        Index("ix_liq_anom_edge_from", "from_id"),
        Index("ix_liq_anom_edge_to", "to_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # "caused_by" | "evolved_into" | "historically_similar" | "preceded"
    # | "destabilized" | "stabilized"
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiquidityIntelligenceHistory(Base):
    """Periodic snapshots of the intelligence engine's own state.

    The Phase-13 evolution timeline + market-cycle decomposition both
    need a history of system-level metrics (synthesized stress, meta-
    health, structural break score, alert saturation, regime mix).
    Persisting them on a slow cadence lets us plot drift and compose
    long-horizon narratives without re-running every aggregation on
    every read.
    """

    __tablename__ = "liquidity_intelligence_history"
    __table_args__ = (
        Index("ix_liq_intel_history_ts", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    synthesized_stress: Mapped[Optional[float]] = mapped_column(Float)
    coordinated_state: Mapped[Optional[str]] = mapped_column(String(48))
    cross_layer_agreement: Mapped[Optional[float]] = mapped_column(Float)
    structural_break_score: Mapped[Optional[float]] = mapped_column(Float)
    meta_confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    meta_intelligence_health: Mapped[Optional[float]] = mapped_column(Float)
    health_state: Mapped[Optional[str]] = mapped_column(String(24))
    risk_state_score: Mapped[Optional[float]] = mapped_column(Float)
    regime_shift_probability: Mapped[Optional[float]] = mapped_column(Float)
    dominant_regime: Mapped[Optional[str]] = mapped_column(String(32))
    # JSON dict of cohort medians for ANALYTICS_METRICS at snapshot time.
    fingerprint_json: Mapped[Optional[str]] = mapped_column(Text)


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


# ── Phase 17 Pass B — Operator persistence ────────────────────────────


class OperatorPriorityHistory(Base):
    """Persistent record of every operator-priority item the engine has
    ever emitted. One row per priority_key (deduped fingerprint). On
    each operator_priorities run the row is upserted: last_seen_at_ms
    advances, occurrence_count increments, current scoring is overwritten.
    When the key disappears from a run, status flips to 'resolved' and
    resolved_at_ms is recorded.

    This replaces the in-memory snapshot diff with durable history so
    lifecycle survives restarts and the operator can query by time
    window (digest, escalation timeline)."""

    __tablename__ = "operator_priority_history"
    __table_args__ = (
        UniqueConstraint("priority_key", name="uq_operator_priority_history_key"),
        Index("ix_operator_priority_history_last_seen", "last_seen_at_ms"),
        Index("ix_operator_priority_history_status_escalation", "current_status", "current_escalation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority_key: Mapped[str] = mapped_column(String(192), nullable=False)
    source_layer: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    # Current scoring (overwritten on each run).
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    current_escalation: Mapped[str] = mapped_column(String(12), nullable=False)
    severity_raw: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_weight: Mapped[float] = mapped_column(Float, default=1.0)
    # Lifecycle from the last computed run.
    current_lifecycle: Mapped[str] = mapped_column(String(16), default="NEW")
    current_status: Mapped[str] = mapped_column(String(12), default="active")  # active | resolved
    # Timing.
    first_seen_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    # Highest priority/severity this key has ever reached.
    peak_priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    peak_escalation: Mapped[str] = mapped_column(String(12), default="NORMAL")
    members_json: Mapped[Optional[str]] = mapped_column(Text)
    extra_json: Mapped[Optional[str]] = mapped_column(Text)


class OperatorPriorityEvent(Base):
    """Append-only log of materially-interesting changes to a priority
    row: first appearance, escalation transitions, large priority jumps,
    resolutions. Used to render the escalation history drawer and to
    answer "what changed in the last 1h/6h/24h"."""

    __tablename__ = "operator_priority_events"
    __table_args__ = (
        Index("ix_operator_priority_events_key_ts", "priority_key", "ts_ms"),
        Index("ix_operator_priority_events_ts_type", "ts_ms", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority_key: Mapped[str] = mapped_column(String(192), nullable=False)
    source_layer: Mapped[str] = mapped_column(String(24), nullable=False)
    # first_seen | escalation_up | escalation_down | priority_jump |
    # resolved | reappeared
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    priority_before: Mapped[Optional[float]] = mapped_column(Float)
    priority_after: Mapped[Optional[float]] = mapped_column(Float)
    escalation_before: Mapped[Optional[str]] = mapped_column(String(12))
    escalation_after: Mapped[Optional[str]] = mapped_column(String(12))
    note: Mapped[Optional[str]] = mapped_column(Text)


class OperatorAcknowledgement(Base):
    """Operator workflow state per priority_key. NOT a trading action —
    these are read-only intent markers ("I've seen this", "mute for X
    minutes"). The engine never acts on them autonomously."""

    __tablename__ = "operator_acknowledgements"
    __table_args__ = (
        Index("ix_operator_ack_key_active", "priority_key", "active"),
        Index("ix_operator_ack_created", "created_at_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority_key: Mapped[str] = mapped_column(String(192), nullable=False)
    # ack | ignore | resolve | mute
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # For 'mute' only. NULL for the others.
    expires_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    note: Mapped[Optional[str]] = mapped_column(Text)
    # Only one row per priority_key has active=True at a time. New ack
    # supersedes the previous (which gets active=False).
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ── Phase 18 — Investigation & Casework Layer ─────────────────────────


class Investigation(Base):
    """An operator-owned investigation case. Aggregates evidence, notes,
    and lifecycle history around a finding worth tracking over time.

    Cases are either operator-created or auto-drafted by the worker when
    crisis_genesis transitions to PRE_CASCADE. Auto-drafts are marked by
    created_by IS NULL and origin_kind='auto_pre_cascade'.
    """

    __tablename__ = "investigations"
    __table_args__ = (
        Index("ix_investigations_status", "status"),
        Index("ix_investigations_severity", "severity"),
        Index("ix_investigations_updated", "updated_at_ms"),
        Index("ix_investigations_origin_fingerprint", "origin_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # info | warn | critical
    severity: Mapped[str] = mapped_column(String(16), default="warn")
    # OPEN | INVESTIGATING | MONITORING | RESOLVED | ARCHIVED
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    # JSON array of free-form tag strings
    tags_json: Mapped[Optional[str]] = mapped_column(Text)
    # NULL = auto-drafted by worker.
    created_by: Mapped[Optional[int]] = mapped_column(Integer)
    # Future-proof: forward owner. Single-user today.
    assigned_to: Mapped[Optional[int]] = mapped_column(Integer)
    # manual | auto_pre_cascade | reopen
    origin_kind: Mapped[str] = mapped_column(String(24), default="manual")
    # Fingerprint used by auto-draft dedup so we don't open N cases for
    # the same PRE_CASCADE state.
    origin_fingerprint: Mapped[Optional[str]] = mapped_column(String(96))
    # Replay deep-link target: epoch-ms the operator should jump to when
    # opening replay. Set on auto-draft (PRE_CASCADE transition time).
    replay_anchor_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Optional replay window around the anchor — operator's saved scope.
    replay_window_start_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    replay_window_end_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Primary symbol of the case (single string for quick chart-jump).
    primary_symbol: Mapped[Optional[str]] = mapped_column(String(32))
    # JSON array of related symbols — secondary chart context.
    related_symbols_json: Mapped[Optional[str]] = mapped_column(Text)
    # JSON array of collaborator user ids — multi-operator handoff state.
    # Owner stays on `assigned_to`; collaborators get notified via mentions
    # in notes but don't gain edit ownership.
    collaborators_json: Mapped[Optional[str]] = mapped_column(Text)
    # Who last performed any audit-logged action on this case. Derived
    # field; updated on each event_log_event call.
    last_touched_by: Mapped[Optional[int]] = mapped_column(Integer)
    last_touched_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Free-text summary required when status transitions to RESOLVED.
    resolution_summary: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InvestigationEvidence(Base):
    """A typed link from an investigation to a piece of upstream evidence:
    an alert, anomaly, operator-priority key, propagation edge, narrative
    section, or arbitrary symbol/transition reference.

    The ref_id and ref_key fields are kept loose on purpose — the upstream
    tables are not formally foreign-keyed because evidence types span
    independent schemas and may include synthetic refs (e.g. a
    operator_priority_key string, or a "symbol::tf::ts" composite). A
    snapshot of the evidence at link time is kept as JSON so the case
    survives upstream pruning.
    """

    __tablename__ = "investigation_evidence"
    __table_args__ = (
        Index("ix_investigation_evidence_case", "investigation_id"),
        Index("ix_investigation_evidence_type_key", "evidence_type", "ref_key"),
        UniqueConstraint(
            "investigation_id", "evidence_type", "ref_key",
            name="uq_investigation_evidence_triple",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # alert | anomaly | operator_priority | propagation_edge | causal_chain
    # | narrative_section | symbol | transition | dependency_cluster | file
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # FK-style ref to the upstream id, when a single integer makes sense
    # (alert.id, anomaly.id). NULL otherwise.
    ref_id: Mapped[Optional[int]] = mapped_column(Integer)
    # Generic string key, used for string-identified references like
    # operator_priority_key or "BTCUSDT::H1::1716700000000". Always set.
    ref_key: Mapped[str] = mapped_column(String(192), nullable=False)
    # Snapshot of the evidence payload at link time (JSON dict).
    snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    linked_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    linked_by: Mapped[Optional[int]] = mapped_column(Integer)


class InvestigationNote(Base):
    """Append-only operator note attached to a case. Notes are never
    edited or deleted — corrections are added as follow-up notes. This is
    a strict audit-friendly property: an investigator can show the full
    history of how their understanding evolved.
    """

    __tablename__ = "investigation_notes"
    __table_args__ = (
        Index("ix_investigation_notes_case_ts", "investigation_id", "created_at_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # note | hypothesis | conclusion | false_positive | needs_monitoring
    # | confirmed_structural | coincidence | comment
    note_type: Mapped[str] = mapped_column(String(32), nullable=False, default="note")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InvestigationReplaySnapshot(Base):
    """Frozen state-of-the-system captured for an investigation case.

    Phase 19 forensic property: when an investigation opens (manual or
    auto-draft), the worker / API captures a single JSON blob carrying
    every intelligence-layer surface the engine was emitting at that
    moment (operator priorities, sanity audit, genesis verdict,
    adaptation modifiers, narrative causality, transitions, structural
    deps, a slice of propagation). This is the FROZEN reference: it
    never changes after capture. Live reconstruction at any other time
    in the case window is rebuilt from the still-retained history
    tables (intel_history, alert_history, anomaly_memory,
    operator_priority_events), so the frozen vs live diff is the
    operator's forensic signal of "what the engine knew then vs. what
    hindsight now shows".

    One row per case (UPSERT). Recapture is explicit — the prior
    captured_at_ms / payload are overwritten only when the operator
    or worker calls replay_capture with `force=True`.
    """

    __tablename__ = "investigation_replay_snapshots"
    __table_args__ = (
        UniqueConstraint("investigation_id", name="uq_inv_replay_snapshot_case"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The "case time" the snapshot represents. Typically the
    # investigation's replay_anchor_ms; may diverge if the operator
    # recaptures at a different cursor.
    anchor_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    # 'auto_create' | 'auto_draft' | 'operator_recapture'
    captured_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="auto_create")
    captured_by: Mapped[Optional[int]] = mapped_column(Integer)
    # Opaque JSON blob — see _replay_capture_payload in research.py for
    # the section keys. Treated as a frozen reference; do not mutate.
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Bytes of payload — surfaced so the operator can see storage cost.
    payload_size: Mapped[int] = mapped_column(Integer, default=0)


class InvestigationEvent(Base):
    """Append-only lifecycle audit log for a case. Captures status
    transitions, severity changes, evidence-link/unlink, note additions,
    and assignment changes. Used by the case timeline (combined with
    upstream timeline via on-read JOIN in research.py).
    """

    __tablename__ = "investigation_events"
    __table_args__ = (
        Index("ix_investigation_events_case_ts", "investigation_id", "ts_ms"),
        Index("ix_investigation_events_ts_type", "ts_ms", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # created | status_change | severity_change | tags_change | assigned
    # | evidence_linked | evidence_unlinked | note_added | resolved
    # | reopened | archived | auto_drafted
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer)
    # JSON payload — shape depends on event_type. E.g. status_change
    # carries {"from": "OPEN", "to": "INVESTIGATING"}.
    payload_json: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
