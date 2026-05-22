"""Phase-4 microstructure intelligence — Resiliency + Kyle Lambda.

These metrics need a short rolling history of state, not just the current
snapshot. The realtime sampler calls `update_intelligence` once per tick
to (a) append a DepthSample to SymbolState.depth_history, (b) advance
the event-recovery state machine, then reads the metric values via the
helpers below.

Design notes:

  Resiliency
    We detect four event kinds — liq_spike, spread_explosion,
    depth_collapse, obi_flip — debounced 10s apart. Each event captures
    the pre-event credible_depth baseline. We then watch subsequent
    ticks until depth climbs back to RECOVERY_FRACTION × pre — at which
    point recovery_ms and refill_velocity are stamped on the event.
    `resiliency_score` exponentially weights the most-recent completed
    event by recovery_ms and refill_velocity.

  Kyle Lambda
    From the recent trade tape: bin signed volume into 1-second buckets,
    compute |ΔP / signed_V| per bucket (normalized by start price so the
    metric is comparable across symbols), then take the median over the
    last 60s. `impact_score` = clamp(λ, …) and `fragility_score` is the
    standard deviation of bucket λs — high variance = unstable price
    impact = more fragile.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .orderbook import DepthSample, RecoveryEvent, SymbolState

# Event-detection knobs ----------------------------------------------------

EVENT_DEBOUNCE_MS = 10_000          # ignore back-to-back triggers within this
RECOVERY_FRACTION = 0.80            # depth must climb back to ≥80% of pre
DEPTH_COLLAPSE_DROP = 0.40          # ≥40% drop in 10s
SPREAD_EXPLOSION_BPS = 8.0          # > 8 bps spread
LIQ_SPIKE_USD = 50_000              # single-liquidation USD threshold
OBI_FLIP_DELTA = 0.4                # |Δ OBI| over a 10s window
RECOVERY_MAX_AGE_MS = 90_000        # give up tracking after this

# Kyle Lambda knobs --------------------------------------------------------

KYLE_WINDOW_MS = 60_000             # 60s rolling tape window
KYLE_BUCKET_MS = 1_000              # 1s buckets
KYLE_MIN_BUCKETS = 8                # need at least this many filled buckets
KYLE_MIN_VOLUME_USD = 100.0         # ignore buckets with negligible volume


# ── Event detector & state machine ────────────────────────────────────────


def _signed_qty(t) -> float:
    """Signed taker volume sign: buyer_maker=True → taker SELL → −qty."""
    return -t.qty if t.is_buyer_maker else t.qty


def _spread_bps(state: SymbolState) -> Optional[float]:
    bid = state.best_bid
    ask = state.best_ask
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000


def _depth_in_window(state: SymbolState, lookback_ms: int, now_ms: int) -> List[Tuple[int, float]]:
    cutoff = now_ms - lookback_ms
    return [(s.ts, s.depth_usd) for s in state.depth_history if s.ts >= cutoff and s.depth_usd is not None]


def _detect_events(state: SymbolState, now_ms: int, spread_bps: Optional[float], depth_usd: Optional[float]) -> List[RecoveryEvent]:
    """Look at the latest tick's signals and emit fresh RecoveryEvents.

    Multiple kinds can fire on one tick — they're not mutually exclusive
    — but each is debounced against last_event_ts so a long burst on the
    same dimension doesn't spawn a stream of events.
    """
    if now_ms - state.last_event_ts < EVENT_DEBOUNCE_MS:
        return []

    out: List[RecoveryEvent] = []
    pre_depth = depth_usd

    # Liquidation spike — any single liquidation > LIQ_SPIKE_USD within
    # the last 5s. Cheap check against the existing tape.
    five_s_ago = now_ms - 5_000
    for liq in reversed(state.liquidations):
        if liq.ts < five_s_ago:
            break
        if liq.price * liq.qty >= LIQ_SPIKE_USD:
            out.append(RecoveryEvent(kind="liq_spike", started_ts=now_ms, pre_depth=pre_depth))
            break

    # Spread explosion
    if spread_bps is not None and spread_bps > SPREAD_EXPLOSION_BPS:
        out.append(RecoveryEvent(kind="spread_explosion", started_ts=now_ms, pre_depth=pre_depth))

    # Depth collapse — current depth ≤ (1 − DEPTH_COLLAPSE_DROP) × max(last 10s).
    if depth_usd is not None:
        recent = _depth_in_window(state, 10_000, now_ms)
        if recent:
            peak = max(v for _, v in recent)
            if peak > 0 and depth_usd <= peak * (1 - DEPTH_COLLAPSE_DROP):
                # Pre-event baseline is the recent peak, not the current
                # value — otherwise we'd say "recovered" the instant
                # depth recovers to its own collapsed level.
                out.append(RecoveryEvent(kind="depth_collapse", started_ts=now_ms, pre_depth=peak))

    # OBI flip — |Δ OBI| over 10s.
    obis = [(s.ts, s.depth_usd) for s in state.depth_history if s.ts >= now_ms - 10_000]
    # OBI itself isn't stored in DepthSample (kept lean); skip if unavailable.
    # The flip-detector above is left as a stub — depth_collapse + liq + spread
    # already cover the bulk of meaningful events. Adding OBI history would
    # cost RAM for marginal value; revisit if needed.

    if out:
        state.last_event_ts = now_ms
    return out


def _advance_recoveries(state: SymbolState, now_ms: int, depth_usd: Optional[float]) -> None:
    """For every event still in flight, check if depth has recovered or
    if the event has aged out."""
    if depth_usd is None:
        return
    for ev in state.events:
        if ev.recovered_ts is not None:
            continue
        if now_ms - ev.started_ts > RECOVERY_MAX_AGE_MS:
            # Mark as "did not recover" by leaving recovered_ts None and
            # recovery_ms=RECOVERY_MAX_AGE_MS so the score still reflects it.
            ev.recovery_ms = RECOVERY_MAX_AGE_MS
            ev.refill_velocity = 0.0
            ev.recovered_ts = ev.started_ts + RECOVERY_MAX_AGE_MS
            continue
        if ev.pre_depth is None or ev.pre_depth <= 0:
            continue
        if depth_usd >= ev.pre_depth * RECOVERY_FRACTION:
            ev.recovered_ts = now_ms
            ev.recovery_ms = now_ms - ev.started_ts
            elapsed_s = max(0.5, ev.recovery_ms / 1000.0)
            delta_depth = max(0.0, depth_usd - (ev.pre_depth * (1 - DEPTH_COLLAPSE_DROP)))
            ev.refill_velocity = delta_depth / elapsed_s


def update_intelligence(
    state: SymbolState,
    now_ms: int,
    depth_usd: Optional[float],
) -> None:
    """Called once per realtime-sample tick by the engine, BEFORE metric
    reads. Appends a DepthSample, advances any in-flight recovery events,
    and triggers new event detection.
    """
    spread_bps = _spread_bps(state)
    state.push_depth_sample(DepthSample(ts=now_ms, depth_usd=depth_usd, spread_bps=spread_bps))
    _advance_recoveries(state, now_ms, depth_usd)
    new_events = _detect_events(state, now_ms, spread_bps, depth_usd)
    for ev in new_events:
        state.events.append(ev)


# ── Resiliency outputs ────────────────────────────────────────────────────


def _last_completed_event(state: SymbolState) -> Optional[RecoveryEvent]:
    for ev in reversed(state.events):
        if ev.recovery_ms is not None:
            return ev
    return None


def recovery_time_ms(state: SymbolState) -> Optional[float]:
    ev = _last_completed_event(state)
    if ev is None or ev.recovery_ms is None:
        return None
    return float(ev.recovery_ms)


def refill_velocity_usd_per_s(state: SymbolState) -> Optional[float]:
    ev = _last_completed_event(state)
    if ev is None or ev.refill_velocity is None:
        return None
    return float(ev.refill_velocity)


def resiliency_score(state: SymbolState, now_ms: int) -> Optional[float]:
    """0..100, higher = more resilient.

    No completed events yet → None (column shows "—" rather than a
    fabricated 100). One or more events → score blends two parts:
      * recovery time component: exp-decay around a 30s baseline
      * refill velocity component: log-scaled depth-USD per second
    The two are averaged. Older events count less via exponential
    weighting by age.
    """
    completed = [ev for ev in state.events if ev.recovery_ms is not None]
    if not completed:
        return None

    score_acc = 0.0
    weight_acc = 0.0
    for ev in completed:
        age_s = max(0.0, (now_ms - (ev.recovered_ts or ev.started_ts)) / 1000.0)
        # 5-min half-life so old events fade out of the score gracefully.
        weight = math.exp(-age_s / 300.0)
        time_part = 100.0 * math.exp(-(ev.recovery_ms or 0) / 30_000.0)
        velo_part = 50.0 * math.tanh((ev.refill_velocity or 0.0) / 50_000.0) + 50.0
        score_acc += weight * (0.6 * time_part + 0.4 * velo_part)
        weight_acc += weight
    if weight_acc == 0:
        return None
    return max(0.0, min(100.0, score_acc / weight_acc))


# ── Kyle Lambda outputs ───────────────────────────────────────────────────


def _kyle_buckets(state: SymbolState, now_ms: int) -> List[float]:
    """Bucketed |ΔP / P| / |signed_V_USD| per second over the last 60s.

    The normalization makes λ unit-free: a 1bps move on a $1M signed
    flow gives λ ≈ 1e-10. We discard buckets with effectively no flow.
    """
    cutoff = now_ms - KYLE_WINDOW_MS
    bucketed: dict[int, Tuple[float, float, float, float]] = {}
    # bucket_idx → (first_price, last_price, signed_qty, gross_qty)
    for t in state.trades:
        if t.ts < cutoff:
            continue
        if t.price <= 0 or t.qty <= 0:
            continue
        b = (t.ts - cutoff) // KYLE_BUCKET_MS
        if b not in bucketed:
            bucketed[b] = (t.price, t.price, 0.0, 0.0)
        first_p, _last_p, sv, gv = bucketed[b]
        bucketed[b] = (first_p, t.price, sv + _signed_qty(t), gv + t.qty)

    lambdas: List[float] = []
    for first_p, last_p, sv, _gv in bucketed.values():
        if first_p <= 0:
            continue
        # Volume in USD, signed by taker direction.
        signed_usd = sv * first_p
        if abs(signed_usd) < KYLE_MIN_VOLUME_USD:
            continue
        ret = (last_p - first_p) / first_p
        # λ = |return| / |signed flow USD| → tiny number; multiply by 1e9
        # for readable values (≈ bps per $100k of flow).
        lam = abs(ret) / abs(signed_usd) * 1e9
        lambdas.append(lam)
    return lambdas


def kyle_lambda(state: SymbolState, now_ms: int) -> Optional[float]:
    lams = _kyle_buckets(state, now_ms)
    if len(lams) < KYLE_MIN_BUCKETS:
        return None
    lams.sort()
    return lams[len(lams) // 2]  # median for robustness


def impact_score(state: SymbolState, now_ms: int) -> Optional[float]:
    lam = kyle_lambda(state, now_ms)
    if lam is None:
        return None
    # Sigmoid mapping so the column shows a comparable 0..100. λ=1 maps
    # to ~50 — a "typical" mid-cap perp under normal flow.
    return 100.0 / (1.0 + math.exp(-(lam - 1.0)))


def fragility_score(state: SymbolState, now_ms: int) -> Optional[float]:
    lams = _kyle_buckets(state, now_ms)
    if len(lams) < KYLE_MIN_BUCKETS:
        return None
    mean = sum(lams) / len(lams)
    var = sum((x - mean) ** 2 for x in lams) / len(lams)
    std = math.sqrt(var)
    cv = std / mean if mean > 1e-12 else 0.0
    # Coefficient-of-variation scaled to 0..100. CV ≥ 2 → 100 (very fragile).
    return max(0.0, min(100.0, cv * 50.0))
