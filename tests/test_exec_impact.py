"""Unit tests for the Realized vs Predicted Impact layer.

Covers the core observational arithmetic — bucket assignment, book walk
(both directions), burst grouping with gap/side rules, settle gating,
pre/post snapshot selection, and rolling aggregation.

No mocks of the broader engine; we construct a plain SymbolState and
push synthetic frames at it.
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime import exec_impact as ei
from kazus_logic.liquidity.realtime.orderbook import SymbolState, Trade


# ── bucket_for ────────────────────────────────────────────────────────────


def test_bucket_for_thresholds():
    assert ei.bucket_for(0) == "S"
    assert ei.bucket_for(ei.BUCKET_M_USD - 1) == "S"
    assert ei.bucket_for(ei.BUCKET_M_USD) == "M"
    assert ei.bucket_for(ei.BUCKET_L_USD - 1) == "M"
    assert ei.bucket_for(ei.BUCKET_L_USD) == "L"
    assert ei.bucket_for(1e9) == "L"


# ── _walk ─────────────────────────────────────────────────────────────────


def test_walk_asks_partial_consumption():
    # asks sorted ascending: 100@2, 101@5, 102@10
    asks = ((100.0, 2.0), (101.0, 5.0), (102.0, 10.0))
    # consume 3 units: 2@100 + 1@101 → vwap = (200+101)/3
    vwap, exhausted = ei._walk(asks, 3.0)
    assert not exhausted
    assert abs(vwap - (200.0 + 101.0) / 3.0) < 1e-9


def test_walk_bids_descending():
    # bids sorted descending: 100@2, 99@5
    bids = ((100.0, 2.0), (99.0, 5.0))
    vwap, exhausted = ei._walk(bids, 4.0)
    assert not exhausted
    assert abs(vwap - (200.0 + 2 * 99.0) / 4.0) < 1e-9


def test_walk_exhausted_when_qty_exceeds_book():
    asks = ((100.0, 1.0), (101.0, 1.0))
    vwap, exhausted = ei._walk(asks, 10.0)
    assert exhausted
    assert vwap is None


def test_walk_zero_book():
    vwap, exhausted = ei._walk((), 1.0)
    assert exhausted
    assert vwap is None


# ── helpers for building state ───────────────────────────────────────────


def _push_book(state: SymbolState, ts: int,
               bids: list[tuple[float, float]],
               asks: list[tuple[float, float]]) -> None:
    state.apply_depth20(bids, asks, ts)


def _push_trade(state: SymbolState, ts: int, price: float, qty: float,
                taker_buy: bool) -> None:
    # taker BUY → is_buyer_maker=False (buyer was taker)
    # taker SELL → is_buyer_maker=True
    state.push_trade(Trade(ts=ts, price=price, qty=qty,
                           is_buyer_maker=not taker_buy))


# ── detect_and_measure_bursts ────────────────────────────────────────────


def test_burst_below_notional_floor_dropped():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 10.0)],
               asks=[(100.0, 5.0), (101.0, 10.0)])
    _push_trade(s, 1100, price=100.0, qty=1.0, taker_buy=True)  # $100
    _push_book(s, 1700, bids=[(100.0, 10.0)], asks=[(101.0, 5.0)])
    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    assert events == []
    # cursor still advanced past the burst
    assert s.exec_cursor_ts == 1100


def test_medium_buy_burst_full_measurement():
    s = SymbolState(symbol="X")
    # Deeper book so it can absorb a $60k burst comfortably.
    _push_book(s, 1000,
               bids=[(99.0, 1000.0)],
               asks=[(100.0, 500.0), (101.0, 500.0), (102.0, 500.0)])

    # Buy burst, qty=600, prices 100 then 101 (matches book walk).
    # notional = 500*100 + 100*101 = 60_100 → bucket M.
    _push_trade(s, 1100, price=100.0, qty=500.0, taker_buy=True)
    _push_trade(s, 1200, price=101.0, qty=100.0, taker_buy=True)

    # post-settle book: mid moved to 101.5
    _push_book(s, 1800,
               bids=[(101.0, 500.0)],
               asks=[(102.0, 500.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    assert len(events) == 1
    e = events[0]
    assert e.side == "buy"
    assert e.bucket == "M"
    assert not e.book_exhausted
    # pre_mid = 99.5; vwap = (500*100 + 100*101)/600 = 100.16667
    # expected_bps ≈ (100.16667 - 99.5)/99.5 * 1e4 ≈ 67.0 bps
    assert e.expected_bps is not None
    assert 60 < e.expected_bps < 75
    # post_mid = 101.5; realized = (101.5 - 99.5)/99.5*1e4 ≈ 201 bps
    assert 195 < e.realized_bps < 210
    # divergence positive (market moved more than book promised)
    assert e.divergence_bps > 0
    assert e.ratio is not None and e.ratio > 1.0


def test_book_exhausted_flag_set_when_qty_exceeds_visible():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 1.0)],
               asks=[(100.0, 1.0)])  # only 1 unit of ask
    # Taker buy for 100 units; way more than the visible book.
    # notional = 100 * 100 = 10_000 → above floor, bucket S
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)
    _push_book(s, 1800, bids=[(105.0, 1.0)], asks=[(106.0, 1.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    assert len(events) == 1
    e = events[0]
    assert e.book_exhausted is True
    assert e.expected_bps is None
    assert e.divergence_bps is None
    assert e.ratio is None
    # realized is still observed (mid moved from 99.5 to 105.5)
    assert e.realized_bps > 0


def test_sell_burst_signed_correctly():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(100.0, 500.0), (99.0, 500.0)],
               asks=[(101.0, 500.0)])
    # Taker sell qty 600 — fills 500@100 + 100@99
    # notional = 500*100 + 100*99 = 59_900 → M bucket
    _push_trade(s, 1100, price=100.0, qty=500.0, taker_buy=False)
    _push_trade(s, 1200, price=99.0, qty=100.0, taker_buy=False)

    # Post-settle: mid down to 98.5
    _push_book(s, 1800,
               bids=[(98.0, 500.0)],
               asks=[(99.0, 500.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    assert len(events) == 1
    e = events[0]
    assert e.side == "sell"
    # expected and realized both positive (signed by taker direction)
    assert e.expected_bps > 0
    assert e.realized_bps > 0


def test_opposite_side_breaks_burst():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 1000.0)],
               asks=[(100.0, 1000.0)])
    # Buy then immediately Sell at same ts gap → two separate bursts.
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)   # $10k
    _push_trade(s, 1150, price=99.0, qty=100.0, taker_buy=False)   # $9.9k
    _push_book(s, 1800, bids=[(99.0, 1000.0)], asks=[(100.0, 1000.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    # Both above floor → expect 2 events, opposite sides.
    assert len(events) == 2
    assert {e.side for e in events} == {"buy", "sell"}


def test_gap_breaks_burst():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 1000.0)],
               asks=[(100.0, 1000.0)])
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)
    # Gap > BURST_GAP_MS — next print is a separate burst
    _push_trade(s, 1100 + ei.BURST_GAP_MS + 50, price=100.0, qty=100.0, taker_buy=True)
    _push_book(s, 2500, bids=[(99.0, 1000.0)], asks=[(100.0, 1000.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=3000)
    assert len(events) == 2


def test_settle_wait_holds_emission():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 1000.0)],
               asks=[(100.0, 1000.0)])
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)

    # now_ms only 200ms after last trade → settle not yet elapsed
    events = ei.detect_and_measure_bursts(s, now_ms=1300)
    assert events == []
    # cursor NOT advanced — we'll see the burst on the next cycle.
    assert s.exec_cursor_ts == 0


def test_missing_pre_snapshot_drops_event_but_advances_cursor():
    s = SymbolState(symbol="X")
    # No book history; trade arrives.
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)
    _push_book(s, 1800, bids=[(99.0, 1000.0)], asks=[(100.0, 1000.0)])

    events = ei.detect_and_measure_bursts(s, now_ms=2200)
    assert events == []
    assert s.exec_cursor_ts == 1100  # advanced even though dropped


# ── rolling_exec_metrics ─────────────────────────────────────────────────


def test_rolling_metrics_sparse_emission():
    s = SymbolState(symbol="X")
    _push_book(s, 1000,
               bids=[(99.0, 1000.0)],
               asks=[(100.0, 500.0), (101.0, 500.0)])
    _push_trade(s, 1100, price=100.0, qty=500.0, taker_buy=True)
    _push_trade(s, 1200, price=101.0, qty=100.0, taker_buy=True)
    _push_book(s, 1800, bids=[(101.0, 500.0)], asks=[(102.0, 500.0)])
    new = ei.detect_and_measure_bursts(s, now_ms=2200)
    s.exec_events.extend(new)

    out = ei.rolling_exec_metrics(s, now_ms=2200)
    names = {n for n, _ in out}
    # Buy-M bucket should be populated; sell-anything should not.
    assert "exec_expected_bps_buy_M" in names
    assert "exec_realized_bps_buy_M" in names
    assert "exec_divergence_bps_buy_M" in names
    assert "exec_count_buy_M" in names
    assert "exec_book_exhausted" in names
    # No sell bucket → no sell metric rows.
    assert not any(n.endswith("_sell_S") for n in names)
    assert not any(n.endswith("_sell_M") for n in names)
    assert not any(n.endswith("_sell_L") for n in names)


def test_rolling_metrics_emits_exhausted_counter():
    s = SymbolState(symbol="X")
    _push_book(s, 1000, bids=[(99.0, 1.0)], asks=[(100.0, 1.0)])
    _push_trade(s, 1100, price=100.0, qty=100.0, taker_buy=True)  # exhausts book
    _push_book(s, 1800, bids=[(105.0, 1.0)], asks=[(106.0, 1.0)])
    new = ei.detect_and_measure_bursts(s, now_ms=2200)
    s.exec_events.extend(new)

    out = dict(ei.rolling_exec_metrics(s, now_ms=2200))
    assert out["exec_book_exhausted"] == 1.0
    # exhausted bursts have no expected metric — only realized + count + exh.
    assert "exec_expected_bps_buy_S" not in out
    assert out["exec_realized_bps_buy_S"] > 0
    assert out["exec_exhausted_buy_S"] == 1.0


def test_rolling_window_prunes_old_events():
    s = SymbolState(symbol="X")
    _push_book(s, 1000, bids=[(99.0, 1000.0)], asks=[(100.0, 500.0), (101.0, 500.0)])
    _push_trade(s, 1100, price=100.0, qty=500.0, taker_buy=True)
    _push_trade(s, 1200, price=101.0, qty=100.0, taker_buy=True)
    _push_book(s, 1800, bids=[(101.0, 500.0)], asks=[(102.0, 500.0)])
    s.exec_events.extend(ei.detect_and_measure_bursts(s, now_ms=2200))
    assert len(s.exec_events) == 1

    # Step forward past the rolling window — event should prune out.
    out = dict(ei.rolling_exec_metrics(s, now_ms=2200 + ei.EVENT_WINDOW_MS + 1))
    assert len(s.exec_events) == 0
    assert out["exec_book_exhausted"] == 0.0
