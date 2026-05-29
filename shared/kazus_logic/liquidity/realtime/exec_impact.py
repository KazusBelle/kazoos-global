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

# ── Execution-validation per-burst states (PHASE 3B) ───────────────────────
# GOVERNANCE: this is the complete, frozen state set. No additional states
# (e.g. CONTAMINATED or similar) may be introduced without a separate
# governance review (lip-governance §2). Each maps to a proximate, observable
# cause in evaluate_burst().
EV_MEASURED = "MEASURED"          # expected + realized both computed
EV_EXHAUSTED = "EXHAUSTED"        # book-walk could not fill total_qty
EV_INSUFFICIENT = "INSUFFICIENT"  # sub-notional-floor / non-positive qty
EV_DROPPED = "DROPPED"            # pre/post depth snapshot missing (book gap)
EV_UNKNOWN = "UNKNOWN"            # degenerate/absent price (mid ≤ 0)

# Neutral divergence descriptors — sign only, no causal claim.
DIV_POSITIVE = "POSITIVE_DIVERGENCE"
DIV_NEGATIVE = "NEGATIVE_DIVERGENCE"

# Exhaustion sub-state.
EXH_WITHIN = "WITHIN_VISIBLE"
EXH_EXHAUSTED = "EXHAUSTED"
EXH_UNDETERMINED = "UNDETERMINED"  # refused before the book-walk


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


@dataclass
class ExecValidation:
    """Per-burst Execution Validation record (PHASE 3B).

    Measures the difference between visible expected impact and realized
    move for ONE settled burst — over the SAME shared burst boundaries as
    PHASE 3A (no second grouping). Refusal-first: every settled burst yields
    a record, including refusal states (UNKNOWN/INSUFFICIENT/DROPPED) and
    EXHAUSTED, never a fabricated value. It does not interpret the cause of
    any divergence (no hidden-liquidity / spoofing / intent / manipulation
    inference) — `divergence_label` is sign only.

    `expected_impact_bps` / `realized_impact_bps` / `divergence_bps` /
    `divergence_label` are None unless `state == MEASURED`. `realized` and
    `divergence` are also None for EXHAUSTED (expected is unknowable when the
    visible book can't fill the burst). All boundaries are exchange-ts based
    (replay-deterministic); `local_recv_ts` is the existing local domain.
    """

    symbol: str
    state: str                          # EV_* — frozen set, see module consts
    burst_start_ts: int
    burst_end_ts: int
    burst_side: str
    burst_notional: float
    expected_impact_bps: Optional[float]
    realized_impact_bps: Optional[float]
    divergence_bps: Optional[float]
    divergence_label: Optional[str]
    exhaustion_state: str               # EXH_*
    ratio: Optional[float]
    local_recv_ts: int

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "execution_validation_state": self.state,
            "burst_start_ts": self.burst_start_ts,
            "burst_end_ts": self.burst_end_ts,
            "burst_side": self.burst_side,
            "burst_notional": self.burst_notional,
            "expected_impact_bps": self.expected_impact_bps,
            "realized_impact_bps": self.realized_impact_bps,
            "divergence_bps": self.divergence_bps,
            "divergence_label": self.divergence_label,
            "exhaustion_state": self.exhaustion_state,
            "local_recv_ts": self.local_recv_ts,
        }


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


def evaluate_burst(state, burst, side: str, now_ms: int) -> ExecValidation:
    """Single measurement core for one settled burst — the only place the
    expected-vs-realized arithmetic lives. Consumed by both `_measure` (which
    feeds the rolling-median ExecEvent path) and `detect_exec_validation_records`
    (PHASE 3B per-burst persistence), so the two can never diverge.

    Refusal-first: returns an ExecValidation with an explicit `state` for every
    settled burst, never None and never a fabricated value. State is assigned
    by proximate observable cause (no interpolation, no fallback, no synthetic
    reconstruction):

      INSUFFICIENT  sub-notional-floor or non-positive qty
      DROPPED       pre/post depth snapshot missing (observable book_history gap)
      UNKNOWN       degenerate/absent price (pre/post mid ≤ 0)
      EXHAUSTED     book-walk over visible top-20 cannot fill total_qty
      MEASURED      expected + realized both computed; divergence labelled by sign
    """
    total_qty = 0.0
    notional = 0.0
    for t in burst:
        total_qty += t.qty
        notional += t.qty * t.price
    start_ts, end_ts = burst[0].ts, burst[-1].ts

    def _rec(state_, *, exp=None, rea=None, div=None, label=None,
             exh=EXH_UNDETERMINED, ratio=None) -> ExecValidation:
        return ExecValidation(
            symbol=state.symbol, state=state_,
            burst_start_ts=start_ts, burst_end_ts=end_ts,
            burst_side=side, burst_notional=notional,
            expected_impact_bps=exp, realized_impact_bps=rea,
            divergence_bps=div, divergence_label=label,
            exhaustion_state=exh, ratio=ratio, local_recv_ts=now_ms,
        )

    if notional < NOTIONAL_FLOOR_USD or total_qty <= 0:
        return _rec(EV_INSUFFICIENT)

    pre = _find_pre_snapshot(state.book_history, start_ts)
    post = _find_post_snapshot(state.book_history, end_ts + SETTLE_MS)
    if pre is None or post is None:
        return _rec(EV_DROPPED)
    if pre.mid <= 0 or post.mid <= 0:
        return _rec(EV_UNKNOWN)

    # Realized mid move, signed positive in the direction of taker pressure.
    if side == "buy":
        realized_bps = (post.mid - pre.mid) / pre.mid * 1e4
        vwap, exhausted = _walk(pre.asks, total_qty)
    else:
        realized_bps = (pre.mid - post.mid) / pre.mid * 1e4
        vwap, exhausted = _walk(pre.bids, total_qty)

    if exhausted or vwap is None:
        # Visible depth exhausted — expected impact is unknowable without
        # extrapolation, which is forbidden. Realized is still observed but
        # not published here (matches the existing ExecEvent contract).
        return _rec(EV_EXHAUSTED, rea=realized_bps, exh=EXH_EXHAUSTED)

    if side == "buy":
        expected_bps = (vwap - pre.mid) / pre.mid * 1e4
    else:
        expected_bps = (pre.mid - vwap) / pre.mid * 1e4

    divergence_bps = realized_bps - expected_bps
    ratio = (realized_bps / expected_bps) if expected_bps >= EXPECTED_FLOOR_BPS else None
    label = DIV_POSITIVE if divergence_bps >= 0 else DIV_NEGATIVE
    return _rec(EV_MEASURED, exp=expected_bps, rea=realized_bps,
                div=divergence_bps, label=label, exh=EXH_WITHIN, ratio=ratio)


def _measure(state, burst, side: str) -> Optional[ExecEvent]:
    """Adapter for the rolling-median path: returns an ExecEvent for
    MEASURED/EXHAUSTED bursts (identical contract as before) and None
    otherwise. All arithmetic is delegated to `evaluate_burst`."""
    r = evaluate_burst(state, burst, side, burst[-1].ts)
    if r.state not in (EV_MEASURED, EV_EXHAUSTED):
        return None
    return ExecEvent(
        ts=burst[-1].ts, side=side, bucket=bucket_for(r.burst_notional),
        notional_usd=r.burst_notional,
        expected_bps=r.expected_impact_bps, realized_bps=r.realized_impact_bps,
        divergence_bps=r.divergence_bps, ratio=r.ratio,
        book_exhausted=(r.state == EV_EXHAUSTED),
    )


def detect_exec_validation_records(state, now_ms: int) -> List[ExecValidation]:
    """PHASE 3B runtime: one ExecValidation per settled burst.

    Walks the SAME shared burst boundaries as `iter_settled_bursts` (the 3A
    primitive), over an independent forward-only cursor `exec_val_cursor_ts`.
    Reuses `evaluate_burst` for the measurement — no second grouping logic and
    no second measurement path. Refusal states are emitted explicitly, one
    record per settled burst (append-only-friendly: bounded by burst events).
    """
    trades = [t for t in state.trades if t.ts > state.exec_val_cursor_ts]
    bursts, advance_to = iter_settled_bursts(trades, now_ms)
    out: List[ExecValidation] = []
    for burst in bursts:
        side = "sell" if burst[0].is_buyer_maker else "buy"
        out.append(evaluate_burst(state, burst, side, now_ms))
    if advance_to is not None:
        state.exec_val_cursor_ts = advance_to
    return out


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
