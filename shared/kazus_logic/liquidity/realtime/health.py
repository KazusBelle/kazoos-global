"""Runtime Health Telemetry (WS_RELIABILITY_001) — diagnostic-only.

Localizes WHERE in the instrumented runtime pipeline forward progress stopped.
It does NOT measure market state or metric correctness, and asserts only
failure boundaries that instrumentation can prove. When signals are not
conclusive it returns DOWNSTREAM_OF_INGEST_SUCCESS ("unknown internal runtime
failure boundary") rather than an unprovable precise diagnosis.

This module is intentionally dependency-free (no engine / metric / websockets
imports) so the classifier is pure and independently testable, and so it
cannot affect any existing output. See docs/lip-runtime-health.md.
"""

from __future__ import annotations

from typing import Optional

# ── Frozen failure-boundary enum ───────────────────────────────────────────
# Any finer label requires a separate governance review (lip-governance §2).
HEALTHY = "HEALTHY"
FEED_NETWORK_SILENCE = "FEED_NETWORK_SILENCE"
CONSUMER_STALL = "CONSUMER_STALL"
PERSISTENCE_BOTTLENECK = "PERSISTENCE_BOTTLENECK"
SCHEDULER_STARVATION = "SCHEDULER_STARVATION"
DOWNSTREAM_OF_INGEST_SUCCESS = "DOWNSTREAM_OF_INGEST_SUCCESS"

FAILURE_BOUNDARIES = frozenset({
    HEALTHY,
    FEED_NETWORK_SILENCE,
    CONSUMER_STALL,
    PERSISTENCE_BOTTLENECK,
    SCHEDULER_STARVATION,
    DOWNSTREAM_OF_INGEST_SUCCESS,
})

# ── Heartbeat cadence + interim thresholds (Class C, uncalibrated) ─────────
HEALTH_INTERVAL_S = 5
HEALTH_INTERVAL_MS = HEALTH_INTERVAL_S * 1_000
LOOP_LAG_HIGH_MS = 1_000      # heartbeat woke this much later than scheduled
MESSAGE_SILENCE_MS = 5_000    # no frame crossed the boundary for this long
SAMPLE_STALE_MS = 5_000       # sampler has not progressed for this long
# Persistence is blamed for loop lag ONLY when flush activity explains at least
# this fraction of the observed lag. Flush *occurrence* alone is insufficient.
PERSISTENCE_LAG_FRACTION = 0.5


def classify_failure_boundary(
    *,
    now_ms: int,
    subscribed_count: int,
    last_ws_message_ms: int,
    last_sample_ms: int,
    flush_started_ms: int,
    flush_completed_ms: int,
    flush_duration_ms: float,
    loop_lag_ms: float,
) -> str:
    """Pure, deterministic classification from persisted numeric fields only.

    Re-running this over a stored `liquidity_runtime_health` row yields the
    same label — that is the replay-determinism guarantee. First match wins;
    the ordering and the DOWNSTREAM_OF_INGEST_SUCCESS default encode the
    governance rule "prefer unknown over incorrect precise diagnosis".
    """
    # 1) Idle by design: demand-driven engine with nothing subscribed → nothing
    #    should be flowing, so staleness is expected, not a failure.
    if subscribed_count <= 0:
        return HEALTHY

    msg_age = (now_ms - last_ws_message_ms) if last_ws_message_ms > 0 else None
    sample_age = (now_ms - last_sample_ms) if last_sample_ms > 0 else None
    loop_starved = loop_lag_ms >= LOOP_LAG_HIGH_MS
    flush_in_flight = flush_started_ms > flush_completed_ms

    # How much of THIS lag window is attributable to flushing? In-flight → the
    # elapsed in-flight time; just-completed within the blocked window → its
    # measured duration; otherwise nothing. Flush OCCURRENCE alone is NOT
    # enough — its duration must explain the MAJORITY of the loop lag before we
    # attribute starvation to persistence (revised WS_RELIABILITY_001 rule).
    if flush_in_flight:
        flush_contribution_ms: float = float(now_ms - flush_started_ms)
    elif flush_completed_ms > 0 and (now_ms - flush_completed_ms) <= (loop_lag_ms + HEALTH_INTERVAL_MS):
        flush_contribution_ms = flush_duration_ms
    else:
        flush_contribution_ms = 0.0
    flush_explains_lag = (
        loop_lag_ms > 0
        and flush_contribution_ms >= PERSISTENCE_LAG_FRACTION * loop_lag_ms
    )

    # 2) Event loop starved. Attribute to persistence ONLY when flush duration
    #    explains the majority of the lag; otherwise the loop was starved by
    #    SOME blocking call we deliberately do NOT name ("the loop was starved",
    #    not "we know what starved it").
    if loop_starved:
        return PERSISTENCE_BOTTLENECK if flush_explains_lag else SCHEDULER_STARVATION

    # 4) No frames crossing the boundary, loop healthy → silence upstream of /
    #    at ingest (Binance vs network vs ingest-read NOT sub-attributed).
    if msg_age is not None and msg_age >= MESSAGE_SILENCE_MS:
        return FEED_NETWORK_SILENCE

    # 5) Frames fresh, loop healthy, but the sampler is not progressing.
    if (
        msg_age is not None and msg_age < MESSAGE_SILENCE_MS
        and sample_age is not None and sample_age >= SAMPLE_STALE_MS
    ):
        return CONSUMER_STALL

    # 6) Everything fresh and healthy.
    if (
        msg_age is not None and msg_age < MESSAGE_SILENCE_MS
        and sample_age is not None and sample_age < SAMPLE_STALE_MS
        and not flush_in_flight
    ):
        return HEALTHY

    # 7) Ingest succeeded but a finer boundary is not provable from the
    #    recorded signals — refuse to guess.
    return DOWNSTREAM_OF_INGEST_SUCCESS


def build_health_row(
    *,
    now_ms: int,
    loop_lag_ms: float,
    subscribed_count: int,
    conn_id: int,
    last_ws_message_ms: int,
    frames_total: int,
    last_sample_ms: int,
    samples_total: int,
    flush_started_ms: int,
    flush_completed_ms: int,
    flush_duration_ms: float,
    flush_rows_total: int,
) -> dict:
    """Assemble one append-only row dict, including the classified boundary.

    Pure: the `failure_boundary` is derived here from exactly the numeric
    fields persisted alongside it, so the row is self-describing and the
    classification is reproducible from the row alone.
    """
    boundary = classify_failure_boundary(
        now_ms=now_ms,
        subscribed_count=subscribed_count,
        last_ws_message_ms=last_ws_message_ms,
        last_sample_ms=last_sample_ms,
        flush_started_ms=flush_started_ms,
        flush_completed_ms=flush_completed_ms,
        flush_duration_ms=flush_duration_ms,
        loop_lag_ms=loop_lag_ms,
    )
    return {
        "ts": now_ms,
        "loop_lag_ms": loop_lag_ms,
        "last_ws_message_ms": last_ws_message_ms,
        "frames_total": frames_total,
        "last_sample_ms": last_sample_ms,
        "samples_total": samples_total,
        "flush_started_ms": flush_started_ms,
        "flush_completed_ms": flush_completed_ms,
        "flush_duration_ms": flush_duration_ms,
        "flush_rows_total": flush_rows_total,
        "conn_id": conn_id,
        "subscribed_count": subscribed_count,
        "failure_boundary": boundary,
    }
