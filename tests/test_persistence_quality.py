"""Persistence Quality — measurement-quality self-assessment for Credible Depth.

Covers the governance-load-bearing behaviours: UNKNOWN propagation, the
INSUFFICIENT (None) vs measured-degradation (low float) distinction, that
missing snapshots and gaps degrade the score, that a stale book zeroes it,
and replay determinism. Pure reads off SymbolState — no DB / IO.
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime.metrics import (
    PQ_FRAME_INTERVAL_MS,
    PQ_MAX_GAP_MS,
    PQ_MIN_FRAMES,
    PQ_STALE_MS,
    PQ_WINDOW_MS,
    persistence_quality,
)
from kazus_logic.liquidity.realtime.orderbook import BookSnapshot, SymbolState

NOW = 10_000_000


def _frame(ts: int) -> BookSnapshot:
    return BookSnapshot(
        ts=ts,
        bids=((100.0, 1.0),),
        asks=((100.2, 1.0),),
        mid=100.1,
    )


def _state(frame_ts, *, with_mid: bool = True) -> SymbolState:
    s = SymbolState(symbol="TEST")
    for ts in frame_ts:
        s.book_history.append(_frame(ts))
    if frame_ts:
        s.last_depth_ts = max(frame_ts)
    if with_mid:
        s.best_bid, s.best_ask = 100.0, 100.2
    return s


def _even_frames(latest: int, n: int, interval: int = PQ_FRAME_INTERVAL_MS):
    """n frames spaced `interval` apart, the most recent at `latest`."""
    return [latest - i * interval for i in range(n)][::-1]


def test_unknown_propagates_when_mid_unavailable():
    # Plenty of frames, but no mid → Credible Depth is UNKNOWN, so its
    # measurement quality is UNKNOWN too (None), never a fabricated number.
    s = _state(_even_frames(NOW, 50), with_mid=False)
    assert persistence_quality(s, NOW) is None


def test_insufficient_frames_returns_none_not_zero():
    # Mid known but too few frames in the window → INSUFFICIENT → None.
    s = _state(_even_frames(NOW, PQ_MIN_FRAMES - 1))
    assert persistence_quality(s, NOW) is None


def test_healthy_sequence_scores_full():
    # A full window of 100ms-cadence frames ending now → 1.0.
    n = PQ_WINDOW_MS // PQ_FRAME_INTERVAL_MS  # exactly the expected count
    s = _state(_even_frames(NOW, n))
    assert persistence_quality(s, NOW) == 1.0


def test_missing_snapshots_degrade_quality():
    # Half the expected frames present (still 100ms-spaced, no gaps, fresh) →
    # coverage ≈ 0.5 → quality strictly below the healthy 1.0, but a real
    # measured float, NOT None.
    n_full = PQ_WINDOW_MS // PQ_FRAME_INTERVAL_MS
    healthy = persistence_quality(_state(_even_frames(NOW, n_full)), NOW)
    half = persistence_quality(_state(_even_frames(NOW, n_full // 2)), NOW)
    assert half is not None
    assert 0.0 < half < healthy


def test_gap_collapses_continuity():
    # A single inter-frame gap ≥ PQ_MAX_GAP_MS zeroes continuity → quality 0.0,
    # and 0.0 (measured-bad) must be distinct from None (no-measurement).
    frames = _even_frames(NOW, 30)            # dense recent run, latest = NOW
    frames.insert(0, frames[0] - PQ_MAX_GAP_MS - 500)  # one isolated early frame
    s = _state(frames)
    q = persistence_quality(s, NOW)
    assert q == 0.0
    assert q is not None


def test_stale_latest_frame_zeroes_freshness():
    # Latest in-window frame is PQ_STALE_MS old → freshness 0 → quality 0.0.
    # (A stalled stream would otherwise make levels look artificially persistent.)
    latest = NOW - PQ_STALE_MS
    s = _state(_even_frames(latest, 30))
    q = persistence_quality(s, NOW)
    assert q == 0.0


def test_measured_zero_is_distinct_from_unknown():
    # Sanity: the degraded cases above return 0.0 (a float), while the
    # no-measurement cases return None. They are different types of answer.
    stale = persistence_quality(_state(_even_frames(NOW - PQ_STALE_MS, 30)), NOW)
    unknown = persistence_quality(_state(_even_frames(NOW, 50), with_mid=False), NOW)
    assert isinstance(stale, float) and stale == 0.0
    assert unknown is None


def test_replay_deterministic():
    s = _state(_even_frames(NOW, 40))
    assert persistence_quality(s, NOW) == persistence_quality(s, NOW)
