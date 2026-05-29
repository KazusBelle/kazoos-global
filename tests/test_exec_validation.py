"""Execution Validation (PHASE 3B) — per-burst expected-vs-realized records.

Covers the state mapping (by proximate observable cause), neutral divergence
labels, that records use the SAME shared burst boundaries as exec_impact /
PHASE 3A (no second grouping), refusal-first emission, and replay determinism.
The existing test_exec_impact.py guards that the _measure refactor is
behaviour-preserving for the rolling-median path.
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime import exec_impact as ei
from kazus_logic.liquidity.realtime.orderbook import SymbolState, Trade

NOW = 5000  # far enough past the prints below that bursts have settled


def _push_book(s, ts, bids, asks):
    s.apply_depth20(bids, asks, ts)


def _push_trade(s, ts, price, qty, taker_buy):
    s.push_trade(Trade(ts=ts, price=price, qty=qty, is_buyer_maker=not taker_buy))


# ── state mapping by proximate observable cause ────────────────────────────


def test_measured_buy_with_positive_divergence():
    s = SymbolState(symbol="X")
    _push_book(s, 1000, bids=[(99.0, 1000.0)],
               asks=[(100.0, 500.0), (101.0, 500.0), (102.0, 500.0)])
    _push_trade(s, 1100, 100.0, 500.0, taker_buy=True)
    _push_trade(s, 1200, 101.0, 100.0, taker_buy=True)  # notional 60_100 → M
    _push_book(s, 1800, bids=[(101.0, 500.0)], asks=[(102.0, 500.0)])  # mid 99.5→101.5

    recs = ei.detect_exec_validation_records(s, now_ms=2200)
    assert len(recs) == 1
    r = recs[0]
    assert r.state == ei.EV_MEASURED
    assert r.exhaustion_state == ei.EXH_WITHIN
    assert r.burst_side == "buy"
    assert r.expected_impact_bps is not None and 60 < r.expected_impact_bps < 75
    assert 195 < r.realized_impact_bps < 210
    # realized > expected → positive divergence
    assert r.divergence_bps > 0
    assert r.divergence_label == ei.DIV_POSITIVE


def test_insufficient_below_notional_floor():
    s = SymbolState(symbol="X")
    _push_book(s, 1000, bids=[(99.0, 10.0)], asks=[(100.0, 5.0)])
    _push_trade(s, 1100, 100.0, 1.0, taker_buy=True)  # $100 < 5_000 floor
    _push_book(s, 1800, bids=[(100.0, 10.0)], asks=[(101.0, 5.0)])

    r = ei.detect_exec_validation_records(s, now_ms=2200)[0]
    assert r.state == ei.EV_INSUFFICIENT
    assert r.exhaustion_state == ei.EXH_UNDETERMINED
    assert r.expected_impact_bps is None and r.realized_impact_bps is None
    assert r.divergence_bps is None and r.divergence_label is None


def test_dropped_on_missing_snapshot():
    s = SymbolState(symbol="X")
    # No book history at all → pre/post snapshot missing → DROPPED.
    _push_trade(s, 1100, 100.0, 100.0, taker_buy=True)  # $10k > floor
    r = ei.detect_exec_validation_records(s, now_ms=2200)[0]
    assert r.state == ei.EV_DROPPED
    assert r.expected_impact_bps is None and r.realized_impact_bps is None


def test_exhausted_when_walk_cannot_fill():
    s = SymbolState(symbol="X")
    _push_book(s, 1000, bids=[(99.0, 1.0)], asks=[(100.0, 1.0)])  # 1 unit only
    _push_trade(s, 1100, 100.0, 100.0, taker_buy=True)  # needs 100 → exhausts
    _push_book(s, 1800, bids=[(105.0, 1.0)], asks=[(106.0, 1.0)])

    r = ei.detect_exec_validation_records(s, now_ms=2200)[0]
    assert r.state == ei.EV_EXHAUSTED
    assert r.exhaustion_state == ei.EXH_EXHAUSTED
    assert r.expected_impact_bps is None       # unknowable without extrapolation
    assert r.divergence_bps is None and r.divergence_label is None


def test_negative_divergence_label():
    s = SymbolState(symbol="X")
    # Deep book → expected impact sizable, but post mid barely moves → realized
    # < expected → negative divergence.
    _push_book(s, 1000, bids=[(99.0, 1000.0)],
               asks=[(100.0, 200.0), (101.0, 200.0), (102.0, 200.0)])
    _push_trade(s, 1100, 100.0, 200.0, taker_buy=True)
    _push_trade(s, 1200, 101.0, 100.0, taker_buy=True)  # walk to ~101 → expected sizable
    _push_book(s, 1800, bids=[(99.4, 100.0)], asks=[(99.6, 100.0)])  # mid ~99.5, ≈ pre

    r = ei.detect_exec_validation_records(s, now_ms=2200)[0]
    assert r.state == ei.EV_MEASURED
    assert r.divergence_bps < 0
    assert r.divergence_label == ei.DIV_NEGATIVE


# ── shared burst boundaries + replay determinism ───────────────────────────


def test_uses_same_shared_burst_boundaries_as_exec_impact():
    # Two bursts split by a side change; 3B must produce one record per burst,
    # matching the boundaries exec_impact (rolling) consumes.
    def build():
        s = SymbolState(symbol="X")
        _push_book(s, 1000, bids=[(99.0, 1000.0)], asks=[(100.0, 1000.0)])
        _push_trade(s, 1100, 100.0, 100.0, taker_buy=True)
        _push_trade(s, 1150, 99.0, 100.0, taker_buy=False)  # side change
        _push_book(s, 1800, bids=[(99.0, 1000.0)], asks=[(100.0, 1000.0)])
        return s
    s1 = build()
    recs = ei.detect_exec_validation_records(s1, now_ms=2200)
    assert len(recs) == 2
    assert {r.burst_side for r in recs} == {"buy", "sell"}
    # cursor advanced to the last settled burst's end
    assert s1.exec_val_cursor_ts == 1150
    # no re-emission on a second pass (forward-only, append-only)
    assert ei.detect_exec_validation_records(s1, now_ms=2200) == []


def test_replay_deterministic():
    def build():
        s = SymbolState(symbol="X")
        _push_book(s, 1000, bids=[(99.0, 1000.0)],
                   asks=[(100.0, 500.0), (101.0, 500.0)])
        _push_trade(s, 1100, 100.0, 500.0, taker_buy=True)
        _push_trade(s, 1200, 101.0, 100.0, taker_buy=True)
        _push_book(s, 1800, bids=[(101.0, 500.0)], asks=[(102.0, 500.0)])
        return s
    r1 = ei.detect_exec_validation_records(build(), now_ms=2200)[0]
    r2 = ei.detect_exec_validation_records(build(), now_ms=2200)[0]
    assert r1.as_row() == r2.as_row()


def test_no_states_beyond_frozen_set():
    # Guard the governance constraint: the only states the runtime can emit.
    allowed = {ei.EV_MEASURED, ei.EV_EXHAUSTED, ei.EV_INSUFFICIENT,
               ei.EV_DROPPED, ei.EV_UNKNOWN}
    assert allowed == {"MEASURED", "EXHAUSTED", "INSUFFICIENT", "DROPPED", "UNKNOWN"}
