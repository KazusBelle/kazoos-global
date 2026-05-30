"""Burst-synchronized resiliency — PHASE 4A hardening of the existing
resiliency primitive.

Measures observable post-impact recovery of credible_depth after each settled
PHASE 3A/3B burst, on the 1 Hz credible_depth series (`depth_history`). It is a
HARDENING of the existing resiliency measurement (recovery_time / refill
velocity already exist in intelligence.py) with: replay-deterministic
append-only per-episode persistence, explicit refusal states, and
synchronization to the shared burst boundaries. It does NOT touch
`resiliency_score` (preserved) and introduces NO new score / ratio / verdict.

Baseline (authorized — Option A): pre-impact baseline = credible_depth of the
book immediately before burst start (`D_pre`). Recovery is measured toward
`RECOVERY_FRACTION × D_pre` starting at `t0 = burst_end_ts + SETTLE_MS`, from
the settled floor `D0`. No interpolation, no synthetic reconstruction.

It measures observable recovery characteristics only — NOT market strength,
bullishness, rebound probability, hidden liquidity, or market-maker intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .burst import SETTLE_MS, iter_settled_bursts
from .intelligence import RECOVERY_FRACTION, RECOVERY_MAX_AGE_MS

# ── States (frozen; refusal-first). MEASURED is the non-refusal outcome; the
# three refusals are exactly the authorized PHASE 4A set. No other states. ──
RES_MEASURED = "MEASURED"
RES_UNKNOWN = "UNKNOWN"            # absent pre-impact snapshot (no D_pre)
RES_INSUFFICIENT = "INSUFFICIENT"  # D_pre present but recovery unmeasurable
RES_DROPPED = "DROPPED"            # observable data gap / WS interruption in-window
RES_STATES = frozenset({RES_MEASURED, RES_UNKNOWN, RES_INSUFFICIENT, RES_DROPPED})

# Interim, Class C, uncalibrated.
RES_PRE_STALENESS_MS = 3_000   # D_pre sample must be ≤ this old vs burst_start
RES_GAP_MS = 5_000             # depth feed stale > this during window → DROPPED


@dataclass
class _Episode:
    """One in-flight recovery measurement, keyed to a settled burst."""
    symbol: str
    burst_start_ts: int
    burst_end_ts: int
    t0_ms: int
    pre_depth: float       # D_pre — pre-burst credible_depth (baseline)
    settle_depth: float    # D0 — credible_depth at t0 (recovery floor)
    target: float          # RECOVERY_FRACTION × D_pre


def _row(symbol, bstart, bend, t0, state, pre, settle, rt, rv, now_ms) -> dict:
    return {
        "symbol": symbol,
        "burst_start_ts": bstart,
        "burst_end_ts": bend,
        "t0_ms": t0,
        "resiliency_state": state,
        "pre_depth": pre,
        "settle_depth": settle,
        "recovery_time_ms": rt,
        "refill_velocity": rv,
        "local_recv_ts": now_ms,
    }


def _asof_depth(state, ts: int, max_staleness_ms: int) -> Optional[float]:
    """credible_depth of the latest depth_history sample with `sample.ts ≤ ts`,
    if it is within `max_staleness_ms`. As-of selection only — no interpolation.
    Returns None when no fresh-enough sample exists."""
    best_ts = None
    best_val = None
    for s in state.depth_history:
        if s.depth_usd is None:
            continue
        if s.ts <= ts and (best_ts is None or s.ts > best_ts):
            best_ts = s.ts
            best_val = s.depth_usd
    if best_ts is None or (ts - best_ts) > max_staleness_ms:
        return None
    return best_val


def _advance(state, now_ms: int, depth_usd: Optional[float]) -> List[dict]:
    """Advance in-flight episodes; emit a record for each that resolves."""
    if not state.resiliency_episodes:
        return []
    closed: List[dict] = []
    from collections import deque
    still_open = deque()
    # Observable data-gap signal: the depth feed went stale during the window.
    feed_gap = (state.last_depth_ts is not None
                and (now_ms - state.last_depth_ts) > RES_GAP_MS)
    for ep in state.resiliency_episodes:
        if feed_gap:
            closed.append(_row(ep.symbol, ep.burst_start_ts, ep.burst_end_ts,
                               ep.t0_ms, RES_DROPPED, ep.pre_depth, ep.settle_depth,
                               None, None, now_ms))
            continue
        if depth_usd is not None and depth_usd >= ep.target:
            rt = float(now_ms - ep.t0_ms)
            rv = (depth_usd - ep.settle_depth) / max(0.5, rt / 1000.0)
            closed.append(_row(ep.symbol, ep.burst_start_ts, ep.burst_end_ts,
                               ep.t0_ms, RES_MEASURED, ep.pre_depth, ep.settle_depth,
                               rt, rv, now_ms))
            continue
        if now_ms - ep.t0_ms > RECOVERY_MAX_AGE_MS:
            # Did not recover within the cap — a measured outcome (not a refusal):
            # recovery_time stamped at the cap, refill velocity 0 (mirrors the
            # existing primitive's age-out semantics). No new state introduced.
            closed.append(_row(ep.symbol, ep.burst_start_ts, ep.burst_end_ts,
                               ep.t0_ms, RES_MEASURED, ep.pre_depth, ep.settle_depth,
                               float(RECOVERY_MAX_AGE_MS), 0.0, now_ms))
            continue
        still_open.append(ep)
    state.resiliency_episodes = still_open
    return closed


def _open_new(state, now_ms: int, depth_usd: Optional[float]) -> List[dict]:
    """Open episodes for newly settled bursts (forward-only cursor). Immediate
    refusal records for UNKNOWN / INSUFFICIENT; otherwise enqueue in-flight."""
    trades = [t for t in state.trades if t.ts > state.resiliency_cursor_ts]
    bursts, advance_to = iter_settled_bursts(trades, now_ms)
    closed: List[dict] = []
    for burst in bursts:
        bstart, bend = burst[0].ts, burst[-1].ts
        t0 = bend + SETTLE_MS
        d_pre = _asof_depth(state, bstart, RES_PRE_STALENESS_MS)
        if d_pre is None or d_pre <= 0:
            # absent pre-impact snapshot → UNKNOWN
            closed.append(_row(state.symbol, bstart, bend, t0, RES_UNKNOWN,
                               None, None, None, None, now_ms))
            continue
        target = RECOVERY_FRACTION * d_pre
        if depth_usd is None:
            # no post-settle depth to anchor the recovery floor → INSUFFICIENT
            closed.append(_row(state.symbol, bstart, bend, t0, RES_INSUFFICIENT,
                               d_pre, None, None, None, now_ms))
            continue
        if depth_usd >= target:
            # book never depleted below the recovery target → nothing to recover
            closed.append(_row(state.symbol, bstart, bend, t0, RES_INSUFFICIENT,
                               d_pre, depth_usd, None, None, now_ms))
            continue
        state.resiliency_episodes.append(_Episode(
            symbol=state.symbol, burst_start_ts=bstart, burst_end_ts=bend,
            t0_ms=t0, pre_depth=d_pre, settle_depth=depth_usd, target=target,
        ))
    if advance_to is not None:
        state.resiliency_cursor_ts = advance_to
    # Safety bound — open episodes resolve within RECOVERY_MAX_AGE_MS; cap guards
    # against unbounded growth if something pathological stalls resolution.
    while len(state.resiliency_episodes) > 200:
        state.resiliency_episodes.popleft()
    return closed


def detect_resiliency(state, now_ms: int, depth_usd: Optional[float]) -> List[dict]:
    """Per-tick entry: resolve in-flight episodes, then open new ones for
    settled bursts. Returns the append-only rows to persist this tick.

    Replay-deterministic: a pure function of (depth_history series, settled
    burst boundaries, now_ms); the persisted row is authoritative for replay.
    """
    closed = _advance(state, now_ms, depth_usd)
    closed += _open_new(state, now_ms, depth_usd)
    return closed
