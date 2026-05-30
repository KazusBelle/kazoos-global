"""Burst-synchronized resiliency (PHASE 4A hardening).

Exercises the episode state machine over synthetic credible_depth series +
settled bursts: the authorized refusal states (UNKNOWN/INSUFFICIENT/DROPPED),
a recovered MEASURED episode, did-not-recover age-out, determinism, the frozen
state set, and that no new score/ratio is introduced.
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime import resiliency as R
from kazus_logic.liquidity.realtime.intelligence import RECOVERY_MAX_AGE_MS
from kazus_logic.liquidity.realtime.orderbook import DepthSample, SymbolState, Trade

BSTART, BEND = 1000, 1100
T0 = BEND + R.SETTLE_MS  # 1600


def _state(pre_depth=None, pre_ts=900):
    s = SymbolState(symbol="X")
    if pre_depth is not None:
        s.depth_history.append(DepthSample(ts=pre_ts, depth_usd=pre_depth, spread_bps=1.0))
    # one settled buy burst (taker buy → is_buyer_maker=False)
    s.trades.append(Trade(ts=BSTART, price=100.0, qty=1.0, is_buyer_maker=False))
    s.trades.append(Trade(ts=BEND, price=100.0, qty=1.0, is_buyer_maker=False))
    return s


def _tick(s, now_ms, depth):
    s.last_depth_ts = now_ms  # fresh feed unless a test overrides
    return R.detect_resiliency(s, now_ms, depth)


# ── refusal states ──────────────────────────────────────────────────────────


def test_unknown_when_no_pre_impact_snapshot():
    s = _state(pre_depth=None)  # no depth sample before burst → no D_pre
    rows = _tick(s, now_ms=3000, depth=400.0)
    assert len(rows) == 1 and rows[0]["resiliency_state"] == R.RES_UNKNOWN
    assert rows[0]["pre_depth"] is None and rows[0]["recovery_time_ms"] is None


def test_insufficient_when_no_depletion():
    s = _state(pre_depth=1000.0)            # target = 800
    rows = _tick(s, now_ms=3000, depth=900.0)  # D0=900 ≥ 800 → nothing to recover
    assert len(rows) == 1 and rows[0]["resiliency_state"] == R.RES_INSUFFICIENT
    assert rows[0]["pre_depth"] == 1000.0 and rows[0]["recovery_time_ms"] is None


def test_dropped_on_feed_gap_during_window():
    s = _state(pre_depth=1000.0)
    assert _tick(s, now_ms=3000, depth=400.0) == []   # open episode (depleted)
    # next tick: depth feed stale (> RES_GAP_MS) → observable gap → DROPPED
    s.last_depth_ts = 1000                              # stale vs now
    rows = R.detect_resiliency(s, now_ms=3000 + R.RES_GAP_MS + 1, depth_usd=410.0)
    assert len(rows) == 1 and rows[0]["resiliency_state"] == R.RES_DROPPED


# ── measured outcomes ───────────────────────────────────────────────────────


def test_measured_recovered():
    s = _state(pre_depth=1000.0)            # target 800, D0 400
    assert _tick(s, now_ms=3000, depth=400.0) == []      # open
    rows = _tick(s, now_ms=4000, depth=900.0)            # ≥ 800 → recovered
    assert len(rows) == 1
    r = rows[0]
    assert r["resiliency_state"] == R.RES_MEASURED
    assert r["recovery_time_ms"] == float(4000 - T0)     # 2400
    assert r["pre_depth"] == 1000.0 and r["settle_depth"] == 400.0
    assert r["refill_velocity"] == (900.0 - 400.0) / ((4000 - T0) / 1000.0)


def test_measured_did_not_recover_ages_out():
    s = _state(pre_depth=1000.0)
    assert _tick(s, now_ms=3000, depth=400.0) == []
    rows = _tick(s, now_ms=T0 + RECOVERY_MAX_AGE_MS + 1, depth=400.0)  # never reached 800
    assert len(rows) == 1
    r = rows[0]
    assert r["resiliency_state"] == R.RES_MEASURED
    assert r["recovery_time_ms"] == float(RECOVERY_MAX_AGE_MS)
    assert r["refill_velocity"] == 0.0


# ── invariants ──────────────────────────────────────────────────────────────


def test_no_reemission_forward_only():
    s = _state(pre_depth=1000.0)
    _tick(s, now_ms=3000, depth=400.0)        # opens; cursor advances past burst
    _tick(s, now_ms=4000, depth=900.0)        # recovers, closes
    # subsequent ticks emit nothing for the same burst
    assert _tick(s, now_ms=5000, depth=950.0) == []
    assert s.resiliency_cursor_ts == BEND


def test_frozen_state_set_and_no_ratio():
    assert R.RES_STATES == {"MEASURED", "UNKNOWN", "INSUFFICIENT", "DROPPED"}
    # PHASE 4A introduces no score/ratio/verdict.
    assert not hasattr(R, "resiliency_ratio")
    assert not any("ratio" in n.lower() for n in dir(R))


def test_determinism():
    a = _tick(_state(pre_depth=1000.0), now_ms=3000, depth=900.0)
    b = _tick(_state(pre_depth=1000.0), now_ms=3000, depth=900.0)
    assert a == b
