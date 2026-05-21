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
from typing import Optional

from .orderbook import SymbolState

# Credible Depth band: ±0.5% around mid. Tighter than typical
# "market-impact" bands, but appropriate for the persistence filter —
# we want to know about resting liquidity at the touch, not several
# percent away where most orders never get hit anyway.
CREDIBLE_BAND_PCT = 0.005
CREDIBLE_MIN_AGE_MS = 400

LIQ_WINDOW_MS = 60_000  # 1-minute rolling


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


def credible_depth_usd(state: SymbolState, now_ms: Optional[int] = None) -> Optional[float]:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    mid = state.mid_price()
    if mid is None:
        return None
    lo = mid * (1 - CREDIBLE_BAND_PCT)
    hi = mid * (1 + CREDIBLE_BAND_PCT)
    s = 0.0
    for price, (qty, first_ts) in state.bids.items():
        if price < lo:
            continue
        if now_ms - first_ts < CREDIBLE_MIN_AGE_MS:
            continue
        s += price * qty
    for price, (qty, first_ts) in state.asks.items():
        if price > hi:
            continue
        if now_ms - first_ts < CREDIBLE_MIN_AGE_MS:
            continue
        s += price * qty
    return s


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
