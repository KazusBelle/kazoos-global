"""
Tests for the OTE-stream setup detector (kazus_logic.compute.detect_setup_events).

Covered scenarios:
  1. Created       — fresh FVG without same-bar reclaim.
  2. Inversion     — body reclaim of a prior unmitigated FVG (one event per zone).
  3. STB           — fresh FVG + same-bar body reclaim. Suppresses Created
                     and Inversion ids on the same bar/FVG.
  4. Created → Inversion across two bars — both events fire on different cycles.
  5. Stack of zones — sequential bars reclaiming different FVGs each get their
                     own Inversion event_id.
  6. OTE-exit reset — events list is empty when not in OTE; worker side
                     (sent_event_ids) reset is exercised in runner integration.
  7. Bullish (long) mirror — detector emits zeroed-mirror geometry.
  8. STB suppresses inv:T — re-running on a later bar where body is still
                     above the same fresh FVG does not produce another event.
"""

from __future__ import annotations

from kazus_logic.compute import (
    SETUP_CREATED,
    SETUP_INVERSION,
    SETUP_STB,
    detect_setup_events,
)
from kazus_logic.engine import Bar, ZoneResult


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def _ote(direction: str = "bearish") -> ZoneResult:
    """A ZoneResult that's in OTE — only the fields read by the detector
    matter (in_ote, direction)."""
    return ZoneResult(
        zone="ote",
        in_ote=True,
        setup=None,
        retracement=0.7,
        direction=direction,
        fib_low=0.0,
        fib_high=1.0,
        ote_low_price=0.0,
        ote_high_price=1.0,
    )


# ---- 1) Created --------------------------------------------------------------

def test_created_fresh_fvg_without_body_reclaim_emits_only_created():
    # Bar i-2: range [100, 102]
    # Bar i-1: filler [101, 103]
    # Bar i:   gap-up FVG (low > i-2.high), but body straddles the FVG
    #          (open < FVG.top), so no body-reclaim → Created, not STB.
    bars = [
        _bar(1000, 100.0, 102.0, 100.0, 101.0),
        _bar(2000, 101.0, 103.0, 101.0, 102.0),
        _bar(3000, 101.5, 110.0, 101.5, 102.5),  # body=[101.5, 102.5], FVG=[102, 101.5]; body_low=101.5 == FVG.top → not >
    ]
    # FVG (bullish): top = bars[2].low = 101.5, bottom = bars[0].high = 102.0
    # Wait — bullish requires bars[i].low > bars[i-2].high. Here 101.5 > 102 is False.
    # Let me re-craft: need bars[2].low > bars[0].high.
    bars = [
        _bar(1000, 100.0, 102.0, 100.0, 101.0),
        _bar(2000, 101.0, 103.0, 101.0, 102.0),
        _bar(3000, 102.5, 110.0, 102.5, 103.0),  # low=102.5 > 102 → bullish FVG=[102.5, 102]; body=[102.5,103]; body_low=102.5 == top=102.5 → reclaim is body_low > top → False
    ]
    events = detect_setup_events(_ote(), bars)
    assert len(events) == 1
    assert events[0].kind == SETUP_CREATED
    assert events[0].event_id == "new:3000"
    assert events[0].fvg_top == 102.5
    assert events[0].fvg_bottom == 102.0


# ---- 2) Inversion ------------------------------------------------------------

def test_inversion_body_reclaim_of_prior_fvg():
    # FVG forms on bar 2, then bar 4 closes with body fully above it.
    # Body reclaim → Inversion event keyed off the FVG's open_ms.
    bars = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.0, 103.0, 102.0, 102.5),  # bullish FVG: low=102 > prev[0].high=101 → top=102, bottom=101
        _bar(4000, 102.5, 103.0, 102.5, 102.9),  # filler, no new FVG (low=102.5 > bar[1].high=101 makes another FVG... let's check)
        _bar(5000, 105.0, 106.0, 105.0, 105.5),  # body=[105, 105.5], reclaims FVG@2 (top=102 < body_low=105)
    ]
    # Bar 3 (idx=3): low=102.5 > bars[1].high=101 → another bullish FVG (102.5, 101). Mitigated? No bar after.
    # Bar 4 (idx=4): low=105 > bars[2].high=103 → fresh bullish FVG on trigger bar; AND body_low=105 > FVG.top=105? FVG is (105, 103). top=105, body_low=105 — not strictly >.
    # So no STB. Reclaim candidates: FVGs at i=2 and i=3 with top<body_low=105.
    # i=2: top=102 < 105 ✓; i=3: top=102.5 < 105 ✓.
    # Closest = highest top → i=3 (bar at ts=4000, top=102.5).
    events = detect_setup_events(_ote(), bars)
    kinds = [e.kind for e in events]
    # Expect Created (fresh FVG on bar 4 not reclaimed) + Inversion on the closest prior FVG.
    assert SETUP_INVERSION in kinds
    inv = next(e for e in events if e.kind == SETUP_INVERSION)
    assert inv.event_id == "inv:4000"
    assert inv.fvg_top == 102.5
    assert inv.fvg_bottom == 101.0


# ---- 3) STB (impulse: fresh FVG + same-bar reclaim) -------------------------

def test_stb_fresh_fvg_with_body_reclaim_suppresses_created_and_inversion():
    # Trigger bar forms a bullish FVG AND its body sits strictly above the FVG.
    # bullish FVG: bars[i].low > bars[i-2].high → top=bars[i].low, bottom=bars[i-2].high.
    # body_low > top means min(open,close) > bars[i].low — the bar opens/closes
    # strictly above its own low (a no-lower-wick bar).
    bars = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.5, 103.0, 102.0, 102.8),  # low=102 > 101 → bullish FVG=(102, 101); body=[102.5, 102.8]; body_low=102.5 > top=102 ✓
    ]
    events = detect_setup_events(_ote(), bars)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == SETUP_STB
    assert ev.event_id == "stb:3000"
    assert ev.fvg_top == 102.0
    assert ev.fvg_bottom == 101.0
    # STB should suppress same-bar Created (new:3000) and the inversion id
    # for the fresh FVG (inv:3000).
    assert "new:3000" in ev.suppresses
    assert "inv:3000" in ev.suppresses


# ---- 4) Scenario 3 (across two bars: created then inversion) -----------------

def test_created_then_inversion_across_two_cycles():
    bars_t = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        # T: forms bullish FVG=(102, 101), body=[101.5, 101.8] — body_low=101.5
        # < top=102 → NOT reclaimed, Created only.
        _bar(3000, 101.5, 103.0, 101.5, 101.8),
    ]
    events_t = detect_setup_events(_ote(), bars_t)
    assert [e.kind for e in events_t] == [SETUP_CREATED]
    assert events_t[0].event_id == "new:3000"

    # T+1: body fully above the FVG — Inversion on the prior zone.
    bars_t1 = bars_t + [
        _bar(4000, 105.0, 106.0, 105.0, 105.5),
    ]
    events_t1 = detect_setup_events(_ote(), bars_t1)
    inv = next((e for e in events_t1 if e.kind == SETUP_INVERSION), None)
    assert inv is not None
    assert inv.event_id == "inv:3000"  # keyed off the FVG anchor's open_ms


# ---- 5) Stack of zones — separate ids per zone -------------------------------

def test_stack_of_zones_emits_separate_inversion_ids_across_bars():
    # Build two bullish FVGs at increasing heights, then two reclaim bars
    # — each at a different "closest" FVG.
    bars_zone1 = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.5, 103.0, 102.5, 102.8),  # FVG@3000 = (102.5, 101)
    ]
    # Reclaim bar T1: body_low between zone1.top (102.5) and zone2.top — picks zone1.
    bars_phase1 = bars_zone1 + [
        _bar(4000, 103.0, 103.5, 103.0, 103.2),  # filler
        _bar(5000, 104.0, 104.5, 103.5, 104.2),  # body=[104, 104.2] > 102.5 → reclaim zone1
    ]
    # Note: bar at 4000 may form another FVG (low=103 > bars[1].high=101 → yes,
    # FVG@4000=(103, 101)). But then closest reclaimed at T1=5000 = zone with
    # highest top among candidates with top<104=103. So reclaim points to FVG@4000.
    events_p1 = detect_setup_events(_ote(), bars_phase1)
    inv_p1 = next(e for e in events_p1 if e.kind == SETUP_INVERSION)
    # Expect closest = FVG@4000 (top=103, the highest top below body_low=104).
    assert inv_p1.event_id == "inv:4000"

    # Now extend with a second zone higher up that gets reclaimed afterwards.
    bars_phase2 = bars_phase1 + [
        _bar(6000, 104.2, 104.5, 104.2, 104.4),
        _bar(7000, 107.0, 107.5, 107.0, 107.3),  # bullish FVG=(107, 104.5); body above prior zones
    ]
    # The new fresh FVG on T2=7000 is (107, 104.5). body_low=107 > top=107? No (==).
    # Actually FVG.top=bars[7000].low=107, body_low=min(107, 107.3)=107 → not strictly >.
    # So no STB. Reclaim candidates with top<107: FVG@3000(top=102.5), FVG@4000(top=103),
    # FVG@5000 (low=103.5 > bars[3000].high=103 ✓ → bullish FVG=(103.5,103); top=103.5),
    # FVG@6000 (low=104.2 > bars[4000].high=103.5 ✓ → bullish FVG=(104.2,103.5); top=104.2).
    # Closest (highest top<107) = FVG@6000 (top=104.2). New event: inv:6000.
    events_p2 = detect_setup_events(_ote(), bars_phase2)
    inv_ids = sorted({e.event_id for e in events_p2 if e.kind == SETUP_INVERSION})
    assert "inv:6000" in inv_ids


# ---- 6) Out of OTE → no events ----------------------------------------------

def test_out_of_ote_emits_no_events():
    out = ZoneResult(
        zone="discount",
        in_ote=False,
        setup=None,
        retracement=0.5,
        direction="bearish",
        fib_low=0.0, fib_high=1.0,
        ote_low_price=0.0, ote_high_price=1.0,
    )
    bars = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.5, 103.0, 102.5, 102.8),
    ]
    assert detect_setup_events(out, bars) == []


# ---- 7) Bullish (long) mirror -----------------------------------------------

def test_bullish_setup_mirrors_geometry():
    # Mirror of the STB case: bearish FVG forms on the trigger bar AND
    # the trigger body sits fully BELOW the fresh bearish FVG.
    # Bearish FVG: bars[i].high < bars[i-2].low → top=bars[i-2].low, bottom=bars[i].high.
    # body_high < bottom (i.e. < bars[i].high) means a no-upper-wick bar.
    bars = [
        _bar(1000, 110.0, 111.0, 109.0, 110.0),
        _bar(2000, 109.5, 110.0, 109.0, 109.5),
        _bar(3000, 107.5, 108.0, 107.0, 107.2),  # high=108 < bars[0].low=109 → bearish FVG=(109,108); body=[107.2,107.5]; body_high=107.5 < bottom=108 ✓
    ]
    events = detect_setup_events(_ote(direction="bullish"), bars)
    assert len(events) == 1
    assert events[0].kind == SETUP_STB
    assert events[0].fvg_top == 109.0
    assert events[0].fvg_bottom == 108.0


# ---- 8) STB suppresses inv:T on subsequent bar ------------------------------
# This is a worker-level concern (sent_event_ids deduping), so we only verify
# that the STB event_ids in `suppresses` exactly match the ids that would be
# computed by the detector for Inversion of the same FVG on a later bar.

def test_event_id_with_symbol_and_timeframe_prefix():
    # When the detector is given symbol+tf (the path used by compute_symbol),
    # event_ids should embed both — guards against any accidental cross-pair
    # / cross-tf collision in shared dedup stores.
    bars = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.5, 103.0, 102.0, 102.8),
    ]
    [stb] = detect_setup_events(_ote(), bars, symbol="BTCUSDT", timeframe="H1")
    assert stb.event_id == "stb:BTCUSDT:H1:3000"
    assert "new:BTCUSDT:H1:3000" in stb.suppresses
    assert "inv:BTCUSDT:H1:3000" in stb.suppresses


def test_stb_suppresses_correct_event_ids():
    bars = [
        _bar(1000, 100.0, 101.0, 100.0, 100.5),
        _bar(2000, 100.5, 101.0, 100.5, 100.8),
        _bar(3000, 102.5, 103.0, 102.0, 102.8),  # STB bar; FVG anchor ts = 3000
    ]
    [stb] = detect_setup_events(_ote(), bars)
    assert stb.kind == SETUP_STB
    # The inv id we expect to suppress = inv:{fvg_anchor_ts} = inv:3000.
    assert "inv:3000" in stb.suppresses
    # And the same-bar created id.
    assert "new:3000" in stb.suppresses
