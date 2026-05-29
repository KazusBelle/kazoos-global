"""Burst Detection — temporally clustered same-side taker activity as
observed by the Binance `@trade` sensor.

This is a measurement primitive, not a signal/alpha/intent/manipulation
layer. A "burst" is a mesoscopic observation of temporal clustering of
same-side taker prints — nothing more. It does NOT measure aggression,
informed flow, intent, future direction, or manipulation.

Sensor note: production runs on the non-aggregated `<s>@trade` stream
(Binance Futures `@aggTrade` is silently unavailable on this network
perimeter — see engine.py). Each `@trade` message is treated as one
atomic sensor event. `burst_trade_count` therefore counts @trade prints,
which is NOT the number of taker orders (one taker order can print across
several price levels) and is sensitive to how the venue emits prints.

Burst formation (identical rule to the Execution Validation layer, which
consumes the same boundaries — this module is the single source of truth):

  * A burst opens with the first print.
  * It extends while the next print is the same side AND arrives within
    BURST_GAP_MS of the previous print (sliding window — each new print
    re-extends the window).
  * It closes on an opposite-side print OR a gap > BURST_GAP_MS.
  * A burst is only "settled" (safe to read) once SETTLE_MS has elapsed
    since its last print, with a tail-of-tape grace: a burst at the very
    end of the visible tape is held one more cycle because more same-side
    prints could still extend it.

Boundaries are exchange-timestamp based and fully replay-deterministic:
reprocessing the same `@trade` sequence yields identical boundaries,
counts, notional and timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# ── Burst-timing constants (canonical here; exec_impact imports these) ─────
BURST_GAP_MS = 250          # same-side prints within this gap → one burst
SETTLE_MS = 500             # wait this long after last print before reading

# ── Refusal-first warmup ───────────────────────────────────────────────────
# Within this window after a symbol's tape (re)starts, earlier prints may be
# unseen (partial tape visibility), so burst boundaries are not yet trusted →
# UNKNOWN. Interim, uncalibrated.
BURST_WARMUP_MS = 2_000

# Refusal states. These propagate downstream as explicit states, never as a
# fabricated burst.
OK = "OK"
UNKNOWN = "UNKNOWN"            # startup warmup / partial tape / nothing observed
INSUFFICIENT = "INSUFFICIENT"  # warmed up but the current window holds no prints
DROPPED = "DROPPED"            # detectable discontinuity (WS reconnect / tape gap)


def iter_settled_bursts(
    trades: Sequence, now_ms: int
) -> Tuple[List[list], Optional[int]]:
    """Group an ascending-ts sequence of trade prints into closed, settled
    same-side bursts.

    Pure and deterministic: no state mutation, no wall clock. Returns
    ``(bursts, advance_to)`` where ``bursts`` is a list of contiguous
    settled bursts (each a list of prints) taken from the front of the
    sequence, and ``advance_to`` is the ts of the last settled burst's last
    print (the value a caller should advance its forward-only cursor to), or
    None when nothing has settled yet.

    Each print is duck-typed: it must expose ``.ts``, ``.is_buyer_maker``,
    ``.qty``, ``.price``. This is the single burst definition shared with
    the Execution Validation layer.
    """
    if not trades:
        return [], None
    bursts: List[list] = []
    advance_to: Optional[int] = None
    n = len(trades)
    i = 0
    while i < n:
        first = trades[i]
        side = "sell" if first.is_buyer_maker else "buy"
        j = i
        while j + 1 < n:
            nxt = trades[j + 1]
            nxt_side = "sell" if nxt.is_buyer_maker else "buy"
            if nxt_side != side:
                break
            if (nxt.ts - trades[j].ts) > BURST_GAP_MS:
                break
            j += 1
        last = trades[j]
        # Not settled yet — stop; we'll see it on a later cycle.
        if (now_ms - last.ts) < SETTLE_MS:
            break
        # Tail-of-tape grace: a burst ending at the visible tape's edge could
        # still grow with more same-side prints — hold one more cycle.
        if j == n - 1 and (now_ms - last.ts) < BURST_GAP_MS + SETTLE_MS:
            break
        bursts.append(list(trades[i:j + 1]))
        advance_to = last.ts
        i = j + 1
    return bursts, advance_to


@dataclass
class BurstRecord:
    """One standalone burst measurement, or an explicit refusal marker.

    For an OK burst every field is populated. For a refusal marker
    (UNKNOWN / INSUFFICIENT / DROPPED) the burst_* fields are None and only
    ``status`` + ``local_recv_ts`` carry meaning. All burst_* boundaries are
    exchange-timestamp based (replay-deterministic); ``local_recv_ts`` is the
    existing local-receive domain (detection time), not a new time domain.
    """

    symbol: str
    status: str
    burst_start_ts: Optional[int]
    burst_end_ts: Optional[int]
    burst_duration_ms: Optional[int]
    burst_trade_count: Optional[int]
    burst_notional: Optional[float]
    burst_side: Optional[str]
    local_recv_ts: int

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "burst_start_ts": self.burst_start_ts,
            "burst_end_ts": self.burst_end_ts,
            "burst_duration_ms": self.burst_duration_ms,
            "burst_trade_count": self.burst_trade_count,
            "burst_notional": self.burst_notional,
            "burst_side": self.burst_side,
            "local_recv_ts": self.local_recv_ts,
        }

    @classmethod
    def from_burst(cls, symbol: str, burst: list, now_ms: int) -> "BurstRecord":
        start = burst[0].ts
        end = burst[-1].ts
        side = "sell" if burst[0].is_buyer_maker else "buy"
        notional = 0.0
        for t in burst:
            notional += t.qty * t.price
        return cls(
            symbol=symbol,
            status=OK,
            burst_start_ts=start,
            burst_end_ts=end,
            burst_duration_ms=end - start,
            burst_trade_count=len(burst),
            burst_notional=notional,
            burst_side=side,
            local_recv_ts=now_ms,
        )

    @classmethod
    def marker(cls, symbol: str, status: str, now_ms: int) -> "BurstRecord":
        return cls(
            symbol=symbol,
            status=status,
            burst_start_ts=None,
            burst_end_ts=None,
            burst_duration_ms=None,
            burst_trade_count=None,
            burst_notional=None,
            burst_side=None,
            local_recv_ts=now_ms,
        )


def _assess(state, now_ms: int) -> Tuple[List[BurstRecord], str]:
    """Refusal-first burst extraction. Returns (records, status).

    Non-OK statuses always return empty records (we refuse rather than
    fabricate). OK returns the settled burst records detected this cycle
    (possibly empty during a healthy lull) and advances the forward-only
    burst cursor on the state.
    """
    trades_all = list(state.trades)
    if not trades_all:
        # Never started → UNKNOWN; started-then-window-emptied → INSUFFICIENT.
        return [], (UNKNOWN if state.tape_started_ts is None else INSUFFICIENT)

    # Startup / post-reconnect warmup: earlier prints may be unseen.
    if state.tape_started_ts is None or (now_ms - state.tape_started_ts) < BURST_WARMUP_MS:
        return [], UNKNOWN

    # Detectable discontinuity (WS reconnect / tape gap) in the unprocessed
    # region: refuse and step the cursor past the gap so no burst spans it.
    if state.tape_gap_ts is not None and state.tape_gap_ts > state.burst_cursor_ts:
        state.burst_cursor_ts = max(state.burst_cursor_ts, state.tape_gap_ts)
        state.tape_gap_ts = None
        return [], DROPPED

    trades = [t for t in trades_all if t.ts > state.burst_cursor_ts]
    bursts, advance_to = iter_settled_bursts(trades, now_ms)
    if advance_to is not None:
        state.burst_cursor_ts = advance_to
    records = [BurstRecord.from_burst(state.symbol, b, now_ms) for b in bursts]
    return records, OK


def detect_bursts(state, now_ms: int) -> List[BurstRecord]:
    """Runtime entry: emit standalone burst records for `state`.

    Returns OK burst records for every settled burst this cycle. On a
    transition INTO a non-OK status it prepends a single explicit refusal
    marker (so refusal states are persisted and visible to downstream
    Execution Validation without writing a row every cycle). Resumption to
    OK is implicit — real burst rows simply resume.
    """
    records, status = _assess(state, now_ms)
    out: List[BurstRecord] = list(records)
    if status != OK and status != state.burst_status:
        out.insert(0, BurstRecord.marker(state.symbol, status, now_ms))
    state.burst_status = status
    return out
