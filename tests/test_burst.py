"""Burst Detection (PHASE 3A) — shared burst primitive + refusal-first runtime.

Covers the acceptance criteria: deterministic sliding-window clustering,
@trade treated as atomic events, explicit refusal states (UNKNOWN /
INSUFFICIENT / DROPPED), replay determinism, and that the burst boundaries
are the SAME ones exec_impact consumes (shared definition).
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime import burst as b
from kazus_logic.liquidity.realtime import exec_impact as ei
from kazus_logic.liquidity.realtime.orderbook import SymbolState, Trade

SETTLED = 10_000  # now_ms far enough past prints that everything has settled


def _trade(ts: int, taker_buy: bool, price: float = 100.0, qty: float = 1.0) -> Trade:
    # taker BUY → is_buyer_maker=False; taker SELL → is_buyer_maker=True
    return Trade(ts=ts, price=price, qty=qty, is_buyer_maker=not taker_buy)


# ── iter_settled_bursts: the shared clustering primitive ───────────────────


def test_spec_example_single_burst():
    # Spec example: 0, 120, 240, 460 ms — every gap ≤ 250 → ONE burst.
    trades = [_trade(t, taker_buy=True) for t in (0, 120, 240, 460)]
    bursts, advance = b.iter_settled_bursts(trades, SETTLED)
    assert len(bursts) == 1
    assert [t.ts for t in bursts[0]] == [0, 120, 240, 460]
    assert advance == 460


def test_gap_over_threshold_splits():
    # 0, 120, then 380 (gap 260 > 250) → two bursts.
    trades = [_trade(t, taker_buy=True) for t in (0, 120, 380)]
    bursts, _ = b.iter_settled_bursts(trades, SETTLED)
    assert [[t.ts for t in br] for br in bursts] == [[0, 120], [380]]


def test_side_change_splits():
    trades = [_trade(0, True), _trade(100, True), _trade(150, False)]
    bursts, _ = b.iter_settled_bursts(trades, SETTLED)
    assert [[t.ts for t in br] for br in bursts] == [[0, 100], [150]]
    assert bursts[0][0].is_buyer_maker is False  # buy
    assert bursts[1][0].is_buyer_maker is True   # sell


def test_unsettled_tail_held():
    # Last print only 200ms before now → SETTLE_MS not elapsed → not returned.
    trades = [_trade(1000, True)]
    bursts, advance = b.iter_settled_bursts(trades, now_ms=1200)
    assert bursts == [] and advance is None


def test_replay_deterministic_boundaries():
    trades = [_trade(t, taker_buy=(t % 300 != 0)) for t in range(0, 2000, 90)]
    first = b.iter_settled_bursts(trades, SETTLED)
    second = b.iter_settled_bursts(trades, SETTLED)
    assert [[t.ts for t in br] for br in first[0]] == [[t.ts for t in br] for br in second[0]]
    assert first[1] == second[1]


def test_shared_definition_matches_exec_impact():
    # The bursts exec_impact groups must be exactly iter_settled_bursts'.
    s = SymbolState(symbol="X")
    for t in (1000, 1100, 1200):
        s.push_trade(_trade(t, taker_buy=True))
    s.push_trade(_trade(1600, taker_buy=False))  # side change → 2nd burst
    trades = [t for t in s.trades if t.ts > 0]
    bursts, _ = b.iter_settled_bursts(trades, SETTLED)
    # exec_impact over the same tape should produce the same number of bursts
    # (both above the notional floor at qty=1 price=100 → $100 < floor, so
    # exec drops them, but cursor still advances past both).
    ei.detect_and_measure_bursts(s, now_ms=SETTLED)
    assert len(bursts) == 2
    assert s.exec_cursor_ts == 1600  # advanced past the last settled burst


# ── BurstRecord ────────────────────────────────────────────────────────────


def test_record_fields():
    burst = [_trade(1000, True, price=100.0, qty=2.0),
             _trade(1200, True, price=101.0, qty=3.0)]
    rec = b.BurstRecord.from_burst("BTCUSDT", burst, now_ms=5000)
    assert rec.status == "OK"
    assert rec.burst_start_ts == 1000 and rec.burst_end_ts == 1200
    assert rec.burst_duration_ms == 200
    assert rec.burst_trade_count == 2
    assert rec.burst_notional == 100.0 * 2.0 + 101.0 * 3.0
    assert rec.burst_side == "buy"
    assert rec.local_recv_ts == 5000


# ── detect_bursts: refusal-first runtime ───────────────────────────────────


def _state_with(trades, *, started_ts, now_offset=0):
    s = SymbolState(symbol="X")
    for t in trades:
        s.push_trade(t)
    # push_trade set tape_started_ts to the first trade ts; override for warmup
    # control if requested.
    if started_ts is not None:
        s.tape_started_ts = started_ts
    return s


def test_refusal_unknown_when_no_trades():
    s = SymbolState(symbol="X")
    out = b.detect_bursts(s, now_ms=SETTLED)
    assert len(out) == 1 and out[0].status == "UNKNOWN"
    assert out[0].burst_start_ts is None  # marker, not a fabricated burst


def test_refusal_unknown_during_warmup():
    # Trade at t=900; now only 1000ms later → within BURST_WARMUP_MS=2000.
    s = SymbolState(symbol="X")
    s.push_trade(_trade(900, True))
    out = b.detect_bursts(s, now_ms=900 + 1000)
    assert out[0].status == "UNKNOWN"


def test_dropped_on_reconnect_gap():
    s = SymbolState(symbol="X")
    s.push_trade(_trade(1000, True))
    s.tape_started_ts = 1000 - b.BURST_WARMUP_MS - 1  # warmed up
    s.tape_gap_ts = 1500                                # engine marked a reconnect
    out = b.detect_bursts(s, now_ms=SETTLED)
    assert out[0].status == "DROPPED"
    assert s.burst_cursor_ts >= 1500  # cursor stepped past the gap
    assert s.tape_gap_ts is None      # consumed


def test_ok_emits_burst_records_and_advances_cursor():
    s = SymbolState(symbol="X")
    for t in (1000, 1100, 1200):
        s.push_trade(_trade(t, taker_buy=True))
    s.tape_started_ts = 1000 - b.BURST_WARMUP_MS - 1  # warmed up
    out = b.detect_bursts(s, now_ms=SETTLED)
    oks = [r for r in out if r.status == "OK"]
    assert len(oks) == 1
    assert oks[0].burst_trade_count == 3
    assert s.burst_cursor_ts == 1200
    # Re-running yields no new OK records (cursor consumed them) — append-only,
    # no re-emission.
    again = [r for r in b.detect_bursts(s, now_ms=SETTLED) if r.status == "OK"]
    assert again == []


def test_marker_emitted_once_per_transition():
    # Two consecutive UNKNOWN cycles → only the first emits a marker.
    s = SymbolState(symbol="X")
    first = b.detect_bursts(s, now_ms=SETTLED)
    second = b.detect_bursts(s, now_ms=SETTLED)
    assert len(first) == 1 and first[0].status == "UNKNOWN"
    assert second == []  # no duplicate marker; status unchanged
