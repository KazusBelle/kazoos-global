"""Runtime Health Telemetry (WS_RELIABILITY_001) — diagnostic-only.

Covers: classifier determinism + reachability of every approved label, the
frozen failure_boundary enum + closure (classify never returns anything else),
build_health_row arithmetic, and module independence (cannot mutate 3A / 3B /
liquidity_samples outputs because it imports none of them).
"""

from __future__ import annotations

import itertools

from kazus_logic.liquidity.realtime import health as h

NOW = 1_000_000

# A baseline "everything healthy" kwargs set; tests override one axis at a time.
_HEALTHY = dict(
    now_ms=NOW,
    subscribed_count=5,
    last_ws_message_ms=NOW - 200,      # frame 200ms ago
    last_sample_ms=NOW - 800,          # sampled 800ms ago
    flush_started_ms=NOW - 4000,
    flush_completed_ms=NOW - 3900,     # flush completed (not in-flight)
    flush_duration_ms=100.0,
    loop_lag_ms=5.0,
)


def _c(**over):
    kw = dict(_HEALTHY)
    kw.update(over)
    return h.classify_failure_boundary(**kw)


# ── reachability of every approved label ───────────────────────────────────


def test_healthy_normal_operation():
    assert _c() == h.HEALTHY


def test_healthy_when_idle_no_subscriptions():
    # Idle by design: staleness is expected, not a failure.
    assert _c(subscribed_count=0, last_ws_message_ms=0, last_sample_ms=0) == h.HEALTHY


def test_feed_network_silence():
    assert _c(last_ws_message_ms=NOW - 9000) == h.FEED_NETWORK_SILENCE


def test_consumer_stall():
    # frames fresh, sampler stale, loop fine
    assert _c(last_sample_ms=NOW - 9000) == h.CONSUMER_STALL


def test_persistence_when_inflight_flush_explains_majority():
    # loop starved 4s; flush in-flight for 3.5s ≥ 0.5×4s → flush explains it.
    assert _c(loop_lag_ms=4000.0, flush_started_ms=NOW - 3500,
              flush_completed_ms=NOW - 9000) == h.PERSISTENCE_BOTTLENECK


def test_persistence_when_completed_flush_explains_majority():
    # loop starved 4s; last flush completed 0.1s ago took 3s ≥ 0.5×4s.
    assert _c(loop_lag_ms=4000.0, flush_started_ms=NOW - 3100,
              flush_completed_ms=NOW - 100, flush_duration_ms=3000.0) == h.PERSISTENCE_BOTTLENECK


def test_scheduler_when_inflight_flush_does_NOT_explain_majority():
    # Revised-rule guard: a flush is in-flight but only 0.3s of a 3s lag →
    # flush OCCURRENCE alone is insufficient → SCHEDULER_STARVATION, not
    # PERSISTENCE.
    assert _c(loop_lag_ms=3000.0, flush_started_ms=NOW - 300,
              flush_completed_ms=NOW - 9000) == h.SCHEDULER_STARVATION


def test_scheduler_starvation_loop_lag_no_inflight_flush():
    # loop starved, no flush in-flight, small recent flush → cannot name cause.
    assert _c(loop_lag_ms=3000.0) == h.SCHEDULER_STARVATION


def test_downstream_of_ingest_success_when_inconclusive():
    # frames fresh, loop fine, but sampler never ran (last_sample_ms=0) and no
    # other conclusive signal → refuse to guess.
    assert _c(last_sample_ms=0) == h.DOWNSTREAM_OF_INGEST_SUCCESS


# ── determinism ─────────────────────────────────────────────────────────────


def test_classifier_deterministic():
    kw = dict(_HEALTHY, loop_lag_ms=3000.0)
    assert h.classify_failure_boundary(**kw) == h.classify_failure_boundary(**kw)


# ── frozen enum + closure ───────────────────────────────────────────────────


def test_frozen_enum_is_exactly_the_approved_six():
    assert h.FAILURE_BOUNDARIES == {
        "HEALTHY",
        "FEED_NETWORK_SILENCE",
        "CONSUMER_STALL",
        "PERSISTENCE_BOTTLENECK",
        "SCHEDULER_STARVATION",
        "DOWNSTREAM_OF_INGEST_SUCCESS",
    }


def test_classifier_closure_only_returns_frozen_labels():
    # Grid over representative values on every axis — the classifier must never
    # emit a label outside the frozen set (no CONTAMINATED or anything else).
    ages = [0, NOW - 9000, NOW - 200]
    lags = [5.0, 3000.0]
    flush_pairs = [(NOW - 4000, NOW - 3900), (NOW - 8000, NOW - 20000), (NOW - 500, NOW - 9000)]
    subs = [0, 5]
    for lwm, lsm, lag, (fs, fc), sub in itertools.product(ages, ages, lags, flush_pairs, subs):
        out = h.classify_failure_boundary(
            now_ms=NOW, subscribed_count=sub,
            last_ws_message_ms=lwm, last_sample_ms=lsm,
            flush_started_ms=fs, flush_completed_ms=fc,
            flush_duration_ms=100.0, loop_lag_ms=lag,
        )
        assert out in h.FAILURE_BOUNDARIES


# ── build_health_row ────────────────────────────────────────────────────────


def test_build_health_row_fields_and_boundary_consistency():
    row = h.build_health_row(
        now_ms=NOW, loop_lag_ms=3000.0, subscribed_count=5, conn_id=3,
        last_ws_message_ms=NOW - 100, frames_total=123,
        last_sample_ms=NOW - 100, samples_total=45,
        flush_started_ms=NOW - 4000, flush_completed_ms=NOW - 3900,
        flush_duration_ms=100.0, flush_rows_total=999,
    )
    # all persisted numerics present
    for k in ("ts", "loop_lag_ms", "last_ws_message_ms", "frames_total",
              "last_sample_ms", "samples_total", "flush_started_ms",
              "flush_completed_ms", "flush_duration_ms", "flush_rows_total",
              "conn_id", "subscribed_count", "failure_boundary"):
        assert k in row
    # the row's boundary is reproducible from the row's own numeric fields
    assert row["failure_boundary"] == h.classify_failure_boundary(
        now_ms=row["ts"], subscribed_count=row["subscribed_count"],
        last_ws_message_ms=row["last_ws_message_ms"], last_sample_ms=row["last_sample_ms"],
        flush_started_ms=row["flush_started_ms"], flush_completed_ms=row["flush_completed_ms"],
        flush_duration_ms=row["flush_duration_ms"], loop_lag_ms=row["loop_lag_ms"],
    )
    # loop lag 3000 with a completed (not in-flight) flush → scheduler starvation
    assert row["failure_boundary"] == h.SCHEDULER_STARVATION


# ── independence (cannot mutate existing outputs) ──────────────────────────


def test_health_module_imports_no_metric_layers():
    # health.py must not import the metric/runtime layers, so it is structurally
    # incapable of altering 3A / 3B / liquidity_samples behaviour.
    import sys
    src = open(h.__file__).read()
    for forbidden in ("exec_impact", "burst", "metrics", "engine", "intelligence"):
        assert f"import {forbidden}" not in src and f"from .{forbidden}" not in src
