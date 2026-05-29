"""Realtime metric computations off SymbolState.

Each function is a pure read of the current state — no mutation, no
I/O. The sampler calls these at ~1Hz and writes the results to the
liquidity_samples table under the metric names below.

Metric design notes:

  obi_rt
    Same formula as the REST OBI, but evaluated against the WS-sourced
    depth20 levels. Range [-1, +1].

  credible_depth
    USD-value of orderbook levels within ±BAND of mid that have lived
    >= MIN_AGE_MS without their quantity changing. The persistence
    filter defeats spoofing (a passive bid that appears for 100ms and
    vanishes contributes ZERO). USD value = price × qty summed across
    sides. Higher = more genuine resting liquidity around mid.

  liq_stress
    Total USD-value of forced liquidations over the last LIQ_WINDOW_MS.
    Useful as a stress / cascade indicator — spikes precede or
    accompany sharp moves.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from .orderbook import SymbolState

# Credible Depth band: ±0.5% around mid. Tighter than typical
# "market-impact" bands, but appropriate for the persistence filter —
# we want to know about resting liquidity at the touch, not several
# percent away where most orders never get hit anyway.
CREDIBLE_BAND_PCT = 0.005
CREDIBLE_MIN_AGE_MS = 400

LIQ_WINDOW_MS = 60_000  # 1-minute rolling

# ── Persistence Quality knobs ─────────────────────────────────────────────
# persistence_quality grades how well Credible Depth could be *measured* at a
# given tick — NOT the market, NOT direction, NOT a manipulation/spoof verdict.
# It is computed purely from data the ingestion layer already holds:
# `state.book_history` (frozen depth20 frames, pushed every frame in
# `apply_depth20`) and `state.last_depth_ts`. No new data source.
#
# All five constants are INTERIM and uncalibrated. PQ_FRAME_INTERVAL_MS encodes
# the `@depth20@100ms` subscription cadence and is the load-bearing assumption:
# if the real arrival cadence differs, `coverage` mis-reads. Treated as a
# Class C calibration item (see lip-validation-and-calibration / lip-governance).
PQ_WINDOW_MS = 5_000        # assessment window (≤ book_history span at 100 ms)
PQ_FRAME_INTERVAL_MS = 100  # expected inter-frame interval (the @100ms cadence)
PQ_MIN_FRAMES = 10          # below this many frames in-window → INSUFFICIENT (None)
PQ_STALE_MS = 1_000         # latest in-window frame older than this → freshness 0
PQ_MAX_GAP_MS = 1_000       # largest inter-frame gap ≥ this → continuity 0


def _ramp_down(x: float, good: float, bad: float) -> float:
    """1.0 while x ≤ good, 0.0 once x ≥ bad, linear in between. Pure."""
    if x <= good:
        return 1.0
    if x >= bad:
        return 0.0
    return 1.0 - (x - good) / (bad - good)


def persistence_quality(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    """Measurement-quality of Credible Depth at this tick, in [0, 1].

    Answers ONLY "how well was Credible Depth measured here?" — never "how
    real is the liquidity", never direction, never event probability. It is a
    measurement self-assessment, not a market observation, and emits no
    manipulation / spoof / fake-liquidity / executable-liquidity verdict.

    Composed (multiplicatively) of three observations over the last
    `PQ_WINDOW_MS` of depth20 frames in `state.book_history`:

      freshness  — is the latest in-window frame current? `_ramp_down(age,
                   PQ_FRAME_INTERVAL_MS, PQ_STALE_MS)`. Guards against a stalled
                   stream making levels look artificially persistent.
      coverage   — observed frames / expected frames over the window
                   (`PQ_WINDOW_MS / PQ_FRAME_INTERVAL_MS`), clipped to 1.0.
                   Captures sequence completeness / missing snapshots.
      continuity — largest inter-frame gap in the window, `_ramp_down(max_gap,
                   PQ_FRAME_INTERVAL_MS, PQ_MAX_GAP_MS)`. Captures the presence
                   of gaps that destabilise the survivorship observation window.

    Result = freshness · coverage · continuity. Any single axis collapsing to 0
    (stale book, or a ≥ PQ_MAX_GAP_MS gap) zeroes the quality — the safe
    direction for a quality gate is to under-claim, never over-claim.

    Returns None in two no-score cases that both **propagate as UNKNOWN**:
      * mid is unavailable → Credible Depth itself is UNKNOWN, so the quality
        of that (non-)measurement is UNKNOWN too.
      * fewer than PQ_MIN_FRAMES frames in the window → INSUFFICIENT data to
        assess the sequence.
    A *measured* degradation returns a low float (e.g. 0.0), which is distinct
    from None and must not be conflated with it: 0.0 = "measured, and bad";
    None = "could not measure / not enough to judge".

    Replay: like Credible Depth, the live computation depends on in-memory
    frame timestamps; the persisted `liquidity_samples` row is authoritative
    for replay. Pure function of (state, now_ms) — deterministic, no wall clock
    beyond the passed `now_ms`, no interpolation, no hidden fallback.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    # UNKNOWN propagation: quality of an unmeasurable Credible Depth is UNKNOWN.
    if state.mid_price() is None:
        return None
    lo = now_ms - PQ_WINDOW_MS
    ts_list = [s.ts for s in state.book_history if lo <= s.ts <= now_ms]
    # INSUFFICIENT: too few frames to assess the sequence → no score (UNKNOWN).
    if len(ts_list) < PQ_MIN_FRAMES:
        return None
    ts_sorted = sorted(ts_list)

    freshness = _ramp_down(now_ms - ts_sorted[-1], PQ_FRAME_INTERVAL_MS, PQ_STALE_MS)

    expected = PQ_WINDOW_MS / PQ_FRAME_INTERVAL_MS
    coverage = min(len(ts_sorted) / expected, 1.0)

    max_gap = max(
        (b - a for a, b in zip(ts_sorted, ts_sorted[1:])),
        default=PQ_FRAME_INTERVAL_MS,
    )
    continuity = _ramp_down(max_gap, PQ_FRAME_INTERVAL_MS, PQ_MAX_GAP_MS)

    return freshness * coverage * continuity


def obi_rt(state: SymbolState) -> Optional[float]:
    if not state.bids or not state.asks:
        return None
    # Top-N by sort. depth20 already gives us only 20 levels so no need
    # to cap further.
    bid_qty = sum(q for q, _ in state.bids.values())
    ask_qty = sum(q for q, _ in state.asks.values())
    total = bid_qty + ask_qty
    if total <= 0:
        return None
    return (bid_qty - ask_qty) / total


def credible_depth_sides(
    state: SymbolState, now_ms: Optional[int] = None
) -> Tuple[Optional[float], Optional[float]]:
    """Per-side persistent visible liquidity, in USD: ``(bid_usd, ask_usd)``.

    Each side is the USD value of levels within ±CREDIBLE_BAND_PCT of mid
    that have survived ``>= CREDIBLE_MIN_AGE_MS`` without their quantity
    changing — the same survivorship filter `credible_depth_usd` applies,
    decomposed by side rather than summed.

    UNKNOWN propagation: both sides are ``None`` when mid is unavailable
    (empty book / no bookTicker) — no fabricated 0. When mid exists but no
    level on a side clears the age filter, that side is ``0.0`` — an
    *observed* absence of persistent visible liquidity, which is distinct
    from UNKNOWN and must not be conflated with it downstream.

    This measures persistent visible liquidity conditions on each side. It
    is not an estimate of true executable liquidity: sub-CREDIBLE_MIN_AGE_MS
    quote behaviour is structurally unresolved at this instrumentation
    surface (depth20, ~1 Hz visible snapshots).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    mid = state.mid_price()
    if mid is None:
        return None, None
    lo = mid * (1 - CREDIBLE_BAND_PCT)
    hi = mid * (1 + CREDIBLE_BAND_PCT)
    bid = 0.0
    for price, (qty, first_ts) in state.bids.items():
        if price < lo:
            continue
        if now_ms - first_ts < CREDIBLE_MIN_AGE_MS:
            continue
        bid += price * qty
    ask = 0.0
    for price, (qty, first_ts) in state.asks.items():
        if price > hi:
            continue
        if now_ms - first_ts < CREDIBLE_MIN_AGE_MS:
            continue
        ask += price * qty
    return bid, ask


def credible_depth_usd(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    """Combined persistent visible liquidity (bid + ask), in USD.

    Unchanged contract: ``None`` when mid is unavailable, else the summed
    survivorship-filtered USD across both sides. Implemented on top of
    `credible_depth_sides` so the combined and per-side outputs cannot
    drift apart.
    """
    bid, ask = credible_depth_sides(state, now_ms)
    if bid is None or ask is None:
        return None
    return bid + ask


def credible_bid_depth_usd(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    """Persistent visible liquidity resting on the bid side (USD).
    ``None`` when mid is unavailable. See `credible_depth_sides`."""
    return credible_depth_sides(state, now_ms)[0]


def credible_ask_depth_usd(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    """Persistent visible liquidity resting on the ask side (USD).
    ``None`` when mid is unavailable. See `credible_depth_sides`."""
    return credible_depth_sides(state, now_ms)[1]


def credible_depth_delta_usd(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    """Observable imbalance between the two sides: ``bid - ask`` (USD), over
    survivorship-filtered levels only.

    Positive → persistent visible liquidity leans to the bid side; negative
    → to the ask side; ``0.0`` → balanced (or both sides empty under a known
    mid). This is a descriptive observable imbalance, *not* a directional
    signal, a structural-irregularity verdict, or an executable-liquidity
    estimate. ``None`` when mid is unavailable — UNKNOWN propagates.
    """
    bid, ask = credible_depth_sides(state, now_ms)
    if bid is None or ask is None:
        return None
    return bid - ask


def liquidation_stress_usd(state: SymbolState, now_ms: Optional[int] = None) -> float:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = now_ms - LIQ_WINDOW_MS
    total = 0.0
    for liq in state.liquidations:
        if liq.ts < cutoff:
            continue
        total += liq.price * liq.qty
    return total
