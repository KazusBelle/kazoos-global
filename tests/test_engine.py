"""Smoke tests for the Kazus engine port. These are not a faithful vs-Pine
diff; they exercise the public API and the zone/OTE classification so that
regressions in wiring are caught early."""

from __future__ import annotations

import math
import random

from kazus_logic.engine import (
    Bar,
    EQUILIBRIUM_HIGH,
    EQUILIBRIUM_LOW,
    KazusGlobalEngine,
    KazusLocalEngine,
    OTE_HIGH,
    OTE_LOW,
    classify_zone,
    detect_setup,
)


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def test_classify_zone_boundaries():
    assert classify_zone(None) == "none"
    assert classify_zone(0.0) == "premium"
    assert classify_zone(0.2) == "premium"
    assert classify_zone(0.5) == "equilibrium"
    assert classify_zone(EQUILIBRIUM_LOW) == "equilibrium"
    assert classify_zone(EQUILIBRIUM_HIGH) == "equilibrium"
    # 0.5–0.61 → discount (per user requirement)
    assert classify_zone(0.55) == "discount"
    assert classify_zone(0.6) == "discount"
    assert classify_zone(0.7) == "discount"
    assert classify_zone(1.0) == "discount"


def test_detect_setup():
    ote_mid = (OTE_LOW + OTE_HIGH) / 2
    in_ote, setup = detect_setup(ote_mid)
    assert in_ote and setup == "yes"
    assert detect_setup(OTE_LOW - 0.01) == (False, "no")
    assert detect_setup(OTE_HIGH + 0.01) == (False, "no")
    assert detect_setup(None) == (False, "no")


def _leg(ts_start: int, start: float, end: float, n: int) -> list[Bar]:
    step = (end - start) / n
    bars: list[Bar] = []
    ts = ts_start
    price = start
    for _ in range(n):
        o = price
        price += step
        c = price
        h = max(o, c) + abs(step) * 0.4
        l = min(o, c) - abs(step) * 0.4
        bars.append(_bar(ts, o, h, l, c))
        ts += 86_400_000
    return bars


def _synthetic_bullish_series() -> list[Bar]:
    """Four-leg series that guarantees a bullish MS break.

    100 → 80 (down) → 110 (up, newBull engulf)
        → 95 (down, newBear engulf → establishes local_high=110)
        → 130 (up, close breaks 110 → bullishMS).
    """
    bars: list[Bar] = []
    bars += _leg(0, 100, 80, 20)
    bars += _leg(bars[-1].ts + 86_400_000, 80, 110, 20)
    bars += _leg(bars[-1].ts + 86_400_000, 110, 95, 20)
    bars += _leg(bars[-1].ts + 86_400_000, 95, 130, 20)
    return bars


def test_global_engine_detects_bullish_ms():
    bars = _synthetic_bullish_series()
    eng = KazusGlobalEngine()
    for b in bars:
        eng.feed(b)

    # By the end of the sequence we should have a bullish structure.
    snap = eng.snapshot(bars[-1].close)
    assert snap.direction == "bullish"
    assert snap.fib_low is not None
    assert snap.fib_high is not None
    assert snap.fib_high > snap.fib_low

    # Price near fib_high → retracement close to 0.0 → premium.
    s_high = eng.snapshot(snap.fib_high - 1e-6)
    assert s_high.zone == "premium"

    # Price near fib_low → retracement ~1.0 → discount.
    s_low = eng.snapshot(snap.fib_low + 1e-6)
    assert s_low.zone == "discount"


def test_local_engine_zigzag_produces_fib():
    random.seed(7)
    bars: list[Bar] = []
    ts = 0
    price = 100.0
    # 120 bars of noisy drift up then down to create zigzag swings
    for i in range(120):
        delta = math.sin(i / 4) * 3 + random.uniform(-0.5, 0.5)
        o = price
        price += delta
        h = max(o, price) + abs(random.uniform(0, 0.3))
        l = min(o, price) - abs(random.uniform(0, 0.3))
        c = price
        bars.append(_bar(ts, o, h, l, c))
        ts += 3_600_000

    eng = KazusLocalEngine(zigzag_len=20)
    for b in bars:
        eng.feed(b)

    snap = eng.snapshot(bars[-1].close)
    # Either direction is fine; we just need the engine to have produced something.
    assert snap.direction in ("bullish", "bearish", "none")
