"""LIQ STRESS restoration + the liq_spike resiliency guard.

Critical scope boundary: re-feeding state.liquidations restores liq_stress ONLY
and must NOT reactivate liq_spike resiliency while LIQ_SPIKE_RESILIENCY_ENABLED
is False. These tests pin both the metric and the guard. The forceOrder parse
test imports the engine (which imports `websockets`) and is skipped where that
library is absent.
"""

from __future__ import annotations

import pytest

from kazus_logic.liquidity.realtime import intelligence as I
from kazus_logic.liquidity.realtime import metrics as M
from kazus_logic.liquidity.realtime.orderbook import DepthSample, Liquidation, SymbolState

NOW = 10_000_000


def _state_with_big_liq():
    s = SymbolState(symbol="X")
    # a single liquidation well above LIQ_SPIKE_USD (100 * 1000 = 100k ≥ 50k)
    s.push_liquidation(Liquidation(ts=NOW, side="SELL", price=100.0, qty=1000.0))
    # a depth sample so depth-based branches have input; last_event_ts=0 → debounce ok
    s.depth_history.append(DepthSample(ts=NOW, depth_usd=500_000.0, spread_bps=1.0))
    return s


# ── liq_stress metric (the restored output) ────────────────────────────────


def test_liq_stress_sums_recent_liquidations():
    s = SymbolState(symbol="X")
    s.push_liquidation(Liquidation(ts=NOW, side="SELL", price=100.0, qty=10.0))
    s.push_liquidation(Liquidation(ts=NOW, side="BUY", price=50.0, qty=4.0))
    assert M.liquidation_stress_usd(s, now_ms=NOW) == 100.0 * 10.0 + 50.0 * 4.0


def test_liq_stress_zero_when_empty():
    assert M.liquidation_stress_usd(SymbolState(symbol="X"), now_ms=NOW) == 0.0


# ── the guard: liq_spike stays disabled while the flag is False ─────────────


def test_no_liq_spike_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(I, "LIQ_SPIKE_RESILIENCY_ENABLED", False)
    s = _state_with_big_liq()
    events = I._detect_events(s, now_ms=NOW, spread_bps=1.0, depth_usd=500_000.0)
    assert not any(e.kind == "liq_spike" for e in events)


def test_liq_spike_fires_only_when_guard_enabled(monkeypatch):
    # Proves the guard IS the gate: same inputs, flag flipped → liq_spike appears.
    monkeypatch.setattr(I, "LIQ_SPIKE_RESILIENCY_ENABLED", True)
    s = _state_with_big_liq()
    events = I._detect_events(s, now_ms=NOW, spread_bps=1.0, depth_usd=500_000.0)
    assert any(e.kind == "liq_spike" for e in events)


def test_guard_default_is_false():
    assert I.LIQ_SPIKE_RESILIENCY_ENABLED is False


# ── forceOrder parse → push_liquidation (engine; skipped without websockets) ─


def test_on_liquidation_frame_feeds_only_tracked_symbols():
    pytest.importorskip("websockets")
    import json
    from kazus_logic.liquidity.realtime.engine import RealtimeEngine

    eng = RealtimeEngine(db_factory=None)
    eng.states["BTCUSDT"] = SymbolState(symbol="BTCUSDT")

    def frame(sym):
        return json.dumps({"e": "forceOrder", "E": NOW, "o": {
            "s": sym, "S": "BUY", "ap": "100.0", "z": "5", "q": "5",
            "p": "100.0", "T": NOW, "X": "FILLED"}})

    eng._on_liquidation_frame(frame("BTCUSDT"))   # tracked
    eng._on_liquidation_frame(frame("DOGEUSDT"))  # NOT tracked → ignored

    assert "DOGEUSDT" not in eng.states
    assert len(eng.states["BTCUSDT"].liquidations) == 1
    liq = eng.states["BTCUSDT"].liquidations[0]
    assert liq.price == 100.0 and liq.qty == 5.0 and liq.side == "BUY"
    assert M.liquidation_stress_usd(eng.states["BTCUSDT"], now_ms=NOW) == 500.0
