"""Realized vs Predicted Impact — execution validation layer.

Forward-only measurement of how trade bursts actually move the market
versus how the visible top-of-book said they should. Measures divergence,
not market truth — see docs/lip-execution-validation.md for the full
contract (boundary statement, blind-spot inventory, vocabulary discipline,
precedence ordering). No predictions, no scores, no auto-conclusions —
only four numbers per event:

  expected_bps   — book-walk impact using the pre-burst top-20 (signed
                   positive in the direction of taker pressure).
  realized_bps   — actual mid move from pre-burst to post-settle, signed
                   the same way as expected.
  divergence_bps — realized − expected. Positive = market moved more
                   than the book promised; negative = less.
  ratio          — realized / expected, computed only when expected
                   exceeds EXPECTED_FLOOR_BPS so it is not noise-amplified.

A fifth, separate observable:

  book_exhausted — flag: burst notional did not fit in the visible top-20.
                   When True, expected_bps / divergence / ratio are None
                   (we honestly cannot compute them) — but the burst is
                   still counted under `exec_book_exhausted`.

Bursts are consecutive same-side taker prints with gap ≤ BURST_GAP_MS.
Bursts below NOTIONAL_FLOOR_USD are skipped as noise. Pre and post
states come from the in-memory book_history ring; if either is missing
the event is dropped honestly rather than approximated.

Sizes use interim absolute USD buckets (S/M/L). Quantile-based,
per-symbol buckets are deferred until enough history is observed.

There is no replay of pre-existing events — L2 book state is not
persisted to disk. The layer measures only events that occur after
subscription, forward-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

# Burst boundaries are owned by `burst.py` — the single source of truth shared
# with Burst Detection (PHASE 3A). We import the grouping primitive and the
# two burst-timing constants so this layer and burst records can never drift.
from .burst import BURST_GAP_MS, SETTLE_MS, iter_settled_bursts


# ── Knobs ─────────────────────────────────────────────────────────────────
# BURST_GAP_MS / SETTLE_MS imported from .burst (re-exported here so existing
# `exec_impact.BURST_GAP_MS` references keep working).

# Bursts below this USD notional are skipped — noise dominates.
NOTIONAL_FLOOR_USD = 5_000.0

# Ratio is reported only when expected_bps is at least this big.
# Below this the denominator is in the noise band of book-walk arithmetic.
EXPECTED_FLOOR_BPS = 0.5

# Interim absolute USD bucket thresholds. Re-evaluated to per-symbol
# quantiles after the layer has accumulated history.
BUCKET_M_USD = 50_000.0
BUCKET_L_USD = 500_000.0

# Rolling window for the published per-(side,bucket) medians.
EVENT_WINDOW_MS = 5 * 60 * 1000


# ── Data ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecEvent:
    """One completed, settled burst with measured impact.

    `expected_bps`, `divergence_bps`, `ratio` are None when the burst
    exhausted the visible book (book_exhausted=True). `realized_bps` is
    always populated — we always observe what the mid did.
    """
    ts: int                       # last_trade.ts (when the burst ended)
    side: str                     # "buy" | "sell" (taker direction)
    bucket: str                   # "S" | "M" | "L"
    notional_usd: float
    expected_bps: Optional[float]
    realized_bps: float
    divergence_bps: Optional[float]
    ratio: Optional[float]
    book_exhausted: bool


# ── Helpers ───────────────────────────────────────────────────────────────


def bucket_for(notional_usd: float) -> str:
    if notional_usd < BUCKET_M_USD:
        return "S"
    if notional_usd < BUCKET_L_USD:
        return "M"
    return "L"


def _walk(levels: Tuple[Tuple[float, float], ...], target_qty: float
          ) -> Tuple[Optional[float], bool]:
    """Walk price-sorted levels consuming target_qty.

    Returns (vwap, exhausted). Exhausted is True when target_qty did
    not fit in the supplied levels. `levels` is assumed already sorted
    in the direction we want to consume (asks ascending for taker-BUY,
    bids descending for taker-SELL).
    """
    remaining = target_qty
    cost = 0.0
    filled = 0.0
    for price, qty in levels:
        if remaining <= 0:
            break
        if price <= 0 or qty <= 0:
            continue
        take = qty if qty < remaining else remaining
        cost += take * price
        filled += take
        remaining -= take
    if remaining > 1e-12 or filled <= 0:
        return None, True
    return cost / filled, False


def _find_pre_snapshot(history, first_trade_ts: int):
    """Latest snapshot with ts <= first_trade_ts. None if not available
    (e.g. burst arrived before any depth20 frame, or pre-state aged out
    of the ring)."""
    chosen = None
    for snap in history:  # iterated oldest -> newest by deque order
        if snap.ts <= first_trade_ts:
            chosen = snap
        else:
            break
    return chosen


def _find_post_snapshot(history, target_ts: int):
    """First snapshot with ts >= target_ts. None if settle hasn't yet
    produced a snapshot (caller should wait, not approximate)."""
    for snap in history:
        if snap.ts >= target_ts:
            return snap
    return None


# ── Burst detection + measurement ─────────────────────────────────────────


def detect_and_measure_bursts(state, now_ms: int) -> List[ExecEvent]:
    """Walk the trade tape from `state.exec_cursor_ts` forward; emit
    completed, settled bursts.

    A burst closes when the next print is opposite-side or > BURST_GAP_MS
    later. We hold off on emitting until `now_ms - last_trade.ts >=
    SETTLE_MS` so the post-mid has time to form. The cursor is advanced
    past any burst we emit OR drop — we never re-evaluate the same prints.
    """
    trades = [t for t in state.trades if t.ts > state.exec_cursor_ts]
    bursts, advance_to = iter_settled_bursts(trades, now_ms)

    emitted: List[ExecEvent] = []
    for burst in bursts:
        side = "sell" if burst[0].is_buyer_maker else "buy"
        evt = _measure(state, burst, side)
        if evt is not None:
            emitted.append(evt)
    # Cursor advances past every settled burst regardless of emission — we
    # never retry drops. `advance_to` is the last settled burst's last ts.
    if advance_to is not None:
        state.exec_cursor_ts = advance_to

    return emitted


def _measure(state, burst, side: str) -> Optional[ExecEvent]:
    """Compute one ExecEvent from a closed burst. Returns None when the
    burst is below the noise floor OR pre/post book state is missing."""
    total_qty = 0.0
    notional = 0.0
    for t in burst:
        total_qty += t.qty
        notional += t.qty * t.price
    if notional < NOTIONAL_FLOOR_USD or total_qty <= 0:
        return None

    pre = _find_pre_snapshot(state.book_history, burst[0].ts)
    post = _find_post_snapshot(state.book_history, burst[-1].ts + SETTLE_MS)
    if pre is None or post is None:
        return None
    if pre.mid <= 0 or post.mid <= 0:
        return None

    # Realized mid move, signed positive in the direction of taker pressure.
    if side == "buy":
        realized_bps = (post.mid - pre.mid) / pre.mid * 1e4
        vwap, exhausted = _walk(pre.asks, total_qty)
    else:
        realized_bps = (pre.mid - post.mid) / pre.mid * 1e4
        vwap, exhausted = _walk(pre.bids, total_qty)

    bucket = bucket_for(notional)
    if exhausted or vwap is None:
        return ExecEvent(
            ts=burst[-1].ts, side=side, bucket=bucket,
            notional_usd=notional,
            expected_bps=None, realized_bps=realized_bps,
            divergence_bps=None, ratio=None,
            book_exhausted=True,
        )

    if side == "buy":
        expected_bps = (vwap - pre.mid) / pre.mid * 1e4
    else:
        expected_bps = (pre.mid - vwap) / pre.mid * 1e4

    divergence_bps = realized_bps - expected_bps
    ratio = (realized_bps / expected_bps) if expected_bps >= EXPECTED_FLOOR_BPS else None

    return ExecEvent(
        ts=burst[-1].ts, side=side, bucket=bucket,
        notional_usd=notional,
        expected_bps=expected_bps, realized_bps=realized_bps,
        divergence_bps=divergence_bps, ratio=ratio,
        book_exhausted=False,
    )


# ── Rolling aggregation for sample publication ────────────────────────────


def _prune(state, now_ms: int) -> None:
    cutoff = now_ms - EVENT_WINDOW_MS
    while state.exec_events and state.exec_events[0].ts < cutoff:
        state.exec_events.popleft()


def _median(xs: List[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 == 1 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def rolling_exec_metrics(state, now_ms: int) -> List[Tuple[str, float]]:
    """Return sparse (metric_name, value) list for the current window.

    Empty buckets are simply not reported — the layer never writes
    fabricated zeros. The global `exec_book_exhausted` counter is
    always reported (0 when no exhausted events in window).
    """
    _prune(state, now_ms)
    out: List[Tuple[str, float]] = []
    by_key: dict = {}
    exhausted_count = 0
    for ev in state.exec_events:
        if ev.book_exhausted:
            exhausted_count += 1
            # Exhausted bursts still belong to a (side, bucket); count
            # them so the operator sees where exhaustion happens.
            key = (ev.side, ev.bucket)
            by_key.setdefault(key, {"exp": [], "rea": [], "div": [], "rat": [], "exh": 0, "n": 0})
            by_key[key]["exh"] += 1
            by_key[key]["n"] += 1
            by_key[key]["rea"].append(ev.realized_bps)
            continue
        key = (ev.side, ev.bucket)
        d = by_key.setdefault(key, {"exp": [], "rea": [], "div": [], "rat": [], "exh": 0, "n": 0})
        d["n"] += 1
        d["exp"].append(ev.expected_bps)
        d["rea"].append(ev.realized_bps)
        d["div"].append(ev.divergence_bps)
        if ev.ratio is not None:
            d["rat"].append(ev.ratio)

    for (side, bucket), d in by_key.items():
        suffix = f"{side}_{bucket}"
        if d["exp"]:
            out.append((f"exec_expected_bps_{suffix}", _median(d["exp"])))
        if d["rea"]:
            out.append((f"exec_realized_bps_{suffix}", _median(d["rea"])))
        if d["div"]:
            out.append((f"exec_divergence_bps_{suffix}", _median(d["div"])))
        if d["rat"]:
            out.append((f"exec_ratio_{suffix}", _median(d["rat"])))
        out.append((f"exec_count_{suffix}", float(d["n"])))
        if d["exh"]:
            out.append((f"exec_exhausted_{suffix}", float(d["exh"])))

    out.append(("exec_book_exhausted", float(exhausted_count)))
    return out
