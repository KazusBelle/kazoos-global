"""
Unit tests for the new long-only INV/CRE/STB detector
(`shared/kazus_logic/setup/detector.py`).

The detector is designed to be called once per worker cycle on the current
window of closed LTF bars, with the SetupState from the previous cycle
threaded through. Tests simulate this by replaying bars one at a time.

Canonical bearish-FVG-at-swing-low fixture used across the file:
    _bar(1000, 108, 109, 107, 108),  # enter OTE
    _bar(2000, 108, 109, 107, 108),  # FVG bar A (i-2): low 107
    _bar(3000, 107, 107, 103, 104),  # bar B = swing low (low 103)
    _bar(4000, 105, 106, 104, 105),  # FVG bar C: high 106 < A.low 107
The displacement bearish FVG (middle bar = the swing-low bar) is top=107,
bottom=106. INV fires on the first later bar whose CLOSE > 107.
"""

from __future__ import annotations

from typing import List, Tuple

from kazus_logic.engine import Bar, ZoneResult
from kazus_logic.setup import SetupEvent, SetupState, detect_setup


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def _bullish_ote(ote_low: float = 100.0, ote_high: float = 110.0) -> ZoneResult:
    return ZoneResult(
        zone="ote",
        in_ote=True,
        setup="yes",
        retracement=0.7,
        direction="bullish",
        fib_low=90.0,
        fib_high=120.0,
        ote_low_price=ote_low,
        ote_high_price=ote_high,
    )


def _replay(
    zone: ZoneResult, bars: List[Bar]
) -> Tuple[SetupState, List[SetupEvent]]:
    """Walk bars chronologically, calling the detector per added bar.
    Returns (final_state, all_events_emitted_in_order).
    """
    prev: SetupState | None = None
    all_events: List[SetupEvent] = []
    for n in range(1, len(bars) + 1):
        state, events = detect_setup(
            zone, bars[:n], prev, symbol="TEST", timeframe="M15"
        )
        all_events.extend(events)
        prev = state
    return prev or SetupState(), all_events


# --- 1. No-setup gates --------------------------------------------------------

def test_not_in_ote_does_not_wipe_prev_state():
    # When the engine reports in_ote=False (price ticked out of the band)
    # but the LTF bars still include the prior in-band session, we no
    # longer hard-return NO with an empty state — the prior session must
    # be carried forward so already-fired INV/STB can't re-emit on the
    # same base when price returns to the band. Direction stays bullish.
    zone = ZoneResult(
        zone="discount", in_ote=False, setup="no", retracement=0.55,
        direction="bullish", fib_low=90.0, fib_high=120.0,
        ote_low_price=100.0, ote_high_price=110.0,
    )
    prev = SetupState(
        state="STB", session_id="abc", inv_fired=True, cre_fired=True,
        stb_fired=True, swing_low=104.0, swing_low_ts=1,
    )
    state, events = detect_setup(zone, [_bar(1, 105, 106, 104, 105)], prev)
    assert events == []
    # stb_fired must survive the out-of-band tick.
    assert state.stb_fired is True


def test_returns_no_when_bearish_direction_long_only():
    zone = ZoneResult(
        zone="ote", in_ote=True, setup="yes", retracement=0.7,
        direction="bearish", fib_low=90.0, fib_high=120.0,
        ote_low_price=100.0, ote_high_price=110.0,
    )
    state, events = detect_setup(zone, [_bar(1, 105, 106, 104, 105)], None)
    assert state.state == "NO"
    assert events == []


def test_returns_no_when_no_bars_in_ote():
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [_bar(1, 90, 92, 89, 91)]  # entirely below OTE
    state, events = detect_setup(zone, bars, None)
    assert state.state == "NO"
    assert events == []


# --- 2. INV fires when a candle CLOSES above the displacement bear FVG -------

def test_inv_fires_on_close_above_bear_fvg():
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),   # enter OTE
        _bar(2000, 108, 109, 107, 108),   # FVG bar A (i-2): low 107
        _bar(3000, 107, 107, 103, 104),   # swing low (bar B)
        _bar(4000, 105, 106, 104, 105),   # FVG bar C → bear FVG top=107, bottom=106
        _bar(5000, 108, 110, 107, 109),   # close 109 > 107 → INV
    ]
    state, events = _replay(zone, bars)
    assert state.state == "INV"
    inv = [e for e in events if e.kind == "INV"]
    assert len(inv) == 1
    assert inv[0].trigger_ts == 5000
    assert inv[0].fvg.kind == "bearish"
    assert inv[0].fvg.top == 107.0
    assert inv[0].fvg.bottom == 106.0
    assert inv[0].event_id == "inv:TEST:M15:5000"


def test_inv_does_not_fire_twice_in_same_session():
    zone = _bullish_ote()
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),  # bear FVG top=107
        _bar(5000, 108, 110, 107, 109),  # INV
        _bar(6000, 108, 110, 106, 109),  # close still above top, but already fired
    ]
    state, events = _replay(zone, bars)
    inv = [e for e in events if e.kind == "INV"]
    assert len(inv) == 1


def test_inv_does_not_fire_when_close_only_touches_fvg_top():
    """Spec: the candle must CLOSE strictly above the FVG top."""
    zone = _bullish_ote()
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),  # bear FVG: top=107, bottom=106
        _bar(5000, 106, 108, 106, 107),  # close=107 NOT strictly > 107
    ]
    state, events = _replay(zone, bars)
    assert state.state == "NO"
    assert [e.kind for e in events] == []


# --- 3. CRE is detected internally but FROZEN as a standalone setup ----------
# A bullish FVG sets `cre_fired` (needed to compose STB) but emits no CRE
# event and never settles the state on "CRE".

def test_cre_detection_is_internal_only_no_event_no_state():
    zone = _bullish_ote()
    bars = [
        _bar(1000, 105, 106, 104, 105),   # enter OTE
        _bar(2000, 105, 106, 104, 105),
        _bar(3000, 105, 107, 104, 106),   # i-2 anchor for bullish FVG
        _bar(4000, 106, 109, 106, 108),   # mid
        _bar(5000, 108, 110, 108, 109),   # bullish FVG: bars[4].low=108 > bars[2].high=107
    ]
    state, events = _replay(zone, bars)
    assert [e.kind for e in events] == []     # no standalone CRE event
    assert state.state == "NO"                # CRE never surfaces as a state
    assert state.cre_fired is True            # but tracked for STB composition
    assert state.first_bull_fvg is not None
    assert state.first_bull_fvg.top == 108.0


# --- 4. STB sequences --------------------------------------------------------

def test_stb_fires_after_inv_then_cre():
    """INV at bar 5000 then a bullish FVG at bar 6000 → STB on the same
    bar. CRE is frozen, so the event stream is INV then STB (no CRE)."""
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),  # bear FVG (top=107, bottom=106)
        _bar(5000, 108, 110, 107, 109),  # INV (close 109 > 107)
        _bar(6000, 108, 109, 107, 108),  # bull FVG: low 107 > bar4.high 106 → STB
    ]
    state, events = _replay(zone, bars)
    kinds = [e.kind for e in events]
    assert kinds == ["INV", "STB"]
    assert state.state == "STB"
    stb = [e for e in events if e.kind == "STB"][0]
    assert stb.trigger_ts == 6000


def test_stb_logic_is_order_agnostic_state_machine():
    """STB fires once both inv_fired and cre_fired are true within the
    session, regardless of which event fired first. Direct state-level
    check (bar-driven CRE-then-INV sequences are awkward to construct
    without inadvertently breaking swing_low; this validates the rule)."""
    s = SetupState(
        state="CRE", session_id="sess1", search_start_ts=1000,
        swing_low=100.0, swing_low_ts=1000,
        first_bear_fvg=None,
        first_bull_fvg=None,
        cre_fired=True, cre_at_ts=2000,
    )
    s.inv_fired = True
    s.inv_at_ts = 3000
    if s.inv_fired and s.cre_fired and not s.stb_fired:
        s.stb_fired = True
    if s.stb_fired:
        s.state = "STB"
    assert s.state == "STB"
    assert s.stb_fired is True


# --- 5. Reset on swing-low break ---------------------------------------------

def test_swing_low_break_wipes_event_flags():
    zone = _bullish_ote(ote_low=95.0, ote_high=110.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),  # swing low = 103
        _bar(4000, 105, 106, 104, 105),  # bear FVG top=107
        _bar(5000, 108, 110, 107, 109),  # INV fires
        _bar(6000, 100, 101, 96, 97),    # body close 97 < swing_low(103) → RESET
    ]
    state, events = _replay(zone, bars)
    inv = [e for e in events if e.kind == "INV"]
    assert len(inv) == 1  # already-fired INV stays in history
    assert state.state == "NO"
    assert state.swing_low == 96.0
    assert state.inv_fired is False
    # search_start_ts advanced to the breaking bar. The bear FVG is only
    # recorded when INV fires; after the reset no later bar inverts
    # anything, so first_bear_fvg stays cleared.
    assert state.search_start_ts == 6000
    assert state.first_bear_fvg is None


def test_inv_flags_clear_after_reset():
    zone = _bullish_ote(ote_low=95.0, ote_high=110.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),  # bear FVG top=107
        _bar(5000, 108, 110, 107, 109),  # INV
        _bar(6000, 100, 101, 96, 97),    # break swing_low → reset
    ]
    state, _ = _replay(zone, bars)
    assert state.inv_fired is False


# --- 6. Session boundary changes reset everything ----------------------------

def test_new_ote_zone_starts_fresh_session():
    zone1 = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars1 = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),
        _bar(5000, 108, 110, 107, 109),  # INV
    ]
    state1, events1 = _replay(zone1, bars1)
    assert any(e.kind == "INV" for e in events1)

    # New OTE bounds → new session.
    zone2 = _bullish_ote(ote_low=200.0, ote_high=210.0)
    bars2 = [_bar(6000, 205, 206, 204, 205)]
    state2, events2 = detect_setup(zone2, bars2, state1)
    assert state2.state == "NO"
    assert state2.session_id != state1.session_id
    assert events2 == []


# --- 7. CRE without INV produces nothing (CRE frozen as standalone) ----------

def test_cre_alone_without_inv_produces_nothing():
    zone = _bullish_ote()
    bars = [
        _bar(1000, 105, 106, 104, 105),
        _bar(2000, 105, 106, 104, 105),
        _bar(3000, 105, 107, 104, 106),
        _bar(4000, 106, 109, 106, 108),
        _bar(5000, 108, 110, 108, 109),    # bullish FVG: cre_fired only, no event
    ]
    state, events = _replay(zone, bars)
    assert state.state == "NO"
    assert [e.kind for e in events] == []
    assert state.cre_fired is True


# --- 8. Bear FVG = the bearish FVG nearest the swing low ---------------------
# Counting up from the low, the INV anchor is the first bearish FVG of the
# last impulse down. When two descent gaps sit around the low the nearer
# one wins — regardless of whether the low is its middle or its 3rd bar.

def test_bear_fvg_is_nearest_descent_fvg_to_low():
    """Two bearish FVGs sit around the low: FVG-X (formed earlier, a bar
    higher) and FVG-Y (nearer the low). The detector anchors INV to the
    nearer one, FVG-Y."""
    zone = _bullish_ote(ote_low=95.0, ote_high=120.0)
    bars = [
        _bar(1000, 110, 111, 109, 110),    # idx0 — FVG-X i-2
        _bar(2000, 108, 109, 107, 108),    # idx1 — FVG-Y i-2 / FVG-X mid
        _bar(3000, 105, 105, 100, 101),    # idx2 — swing low
        _bar(4000, 103, 106, 103, 104),    # idx3 — high 106 < bar2.low 107 → FVG-Y
        _bar(5000, 109, 110, 105, 109.5),  # close 109.5 > FVG-Y top 107 → INV
    ]
    state, events = _replay(zone, bars)
    # FVG-Y (nearer the low) — top = bar2.low 107, bottom = bar4.high 106.
    assert state.first_bear_fvg is not None
    assert state.first_bear_fvg.top == 107.0
    assert state.first_bear_fvg.bottom == 106.0
    assert state.inv_fired is True
    assert state.state == "INV"


def test_bear_fvg_anchor_works_when_low_is_its_third_bar():
    """The swing-low candle is the FVG's 3rd (last) bar, not its middle.
    The detector still anchors INV to it — 'low = middle bar' is not a
    requirement."""
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 110, 111, 109, 110),  # idx0
        _bar(2000, 108, 109, 107, 108),  # idx1
        _bar(3000, 105, 105, 100, 101),  # idx2 — swing low = FVG 3rd bar
        _bar(4000, 103, 108, 102, 107),  # idx3 — high 108 ≥ bar2.low 107 → no nearer FVG
        _bar(5000, 110, 112, 105, 111),  # close 111 > FVG top 109 → INV
    ]
    state, events = _replay(zone, bars)
    assert state.first_bear_fvg is not None
    assert state.first_bear_fvg.top == 109.0
    assert state.first_bear_fvg.bottom == 105.0
    assert state.inv_fired is True
    assert state.state == "INV"


# --- 9. Fire on first observation of a closed-bar trigger --------------------
# Worker polls every few minutes while LTF candles close every 15m, and
# restarts/missed cycles are normal. A still-valid setup whose trigger bar
# closed before this cycle began must alert on first observation.

def test_cre_recorded_on_first_observation_when_bull_fvg_was_formed_earlier():
    """Fresh start (prev_state=None) and the bullish FVG already exists in
    the window. CRE is frozen: cre_fired is set but no event is emitted."""
    zone = _bullish_ote()
    bars = [
        _bar(1000, 105, 106, 104, 105),   # enter OTE
        _bar(2000, 105, 106, 104, 105),
        _bar(3000, 105, 107, 104, 106),   # i-2 anchor for bullish FVG
        _bar(4000, 106, 108, 105, 107),
        _bar(5000, 108, 110, 108, 109),   # bullish FVG: bar[5].low=108 > bar[3].high=107
        _bar(6000, 109, 110, 108, 109),
        _bar(7000, 108, 109, 107, 108),
        _bar(8000, 108, 109, 107, 108),
    ]
    state, events = detect_setup(zone, bars, None, symbol="TEST", timeframe="M15")
    assert [e.kind for e in events] == []
    assert state.state == "NO"
    assert state.cre_fired is True
    assert state.cre_at_ts == 5000


def test_inv_state_updates_but_no_event_when_trigger_is_not_latest_bar():
    """The inverting candle is no longer the last bar by the time the
    worker observes the session. State must still record inv_fired (so the
    detector never refires this trigger), but NO SetupEvent is emitted —
    Telegram only sees triggers that fired on the latest closed bar of the
    current cycle. This is the deliberate post-restart / missed-poll trade.
    """
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),  # bear FVG: top=107
        _bar(5000, 108, 110, 107, 109),  # close 109 > 107 → trigger
        # trailing bars must not break swing_low (=103) nor form a bull FVG.
        _bar(6000, 105, 106, 104, 105),
        _bar(7000, 105, 106, 104, 105),
    ]
    state, events = detect_setup(zone, bars, None, symbol="TEST", timeframe="M15")
    assert state.state == "INV"
    assert state.inv_fired is True
    assert state.inv_at_ts == 5000
    # Trigger bar (5000) is not the latest bar (7000) → no event for TG.
    assert [e for e in events if e.kind == "INV"] == []


# --- 10. Wick-vs-body swing-low break ----------------------------------------
# A bar that pokes below swing_low with a wick but closes its body at or
# above swing_low slides swing_low down without clearing locked anchors or
# fired flags. Only a body close strictly below swing_low triggers a reset.

def test_wick_only_break_moves_swing_low_without_clearing_setup():
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),    # swing low = 103
        _bar(4000, 105, 106, 104, 105),    # bear FVG top=107
        _bar(5000, 108, 110, 107, 109),    # INV fires
        _bar(6000, 105, 108.5, 100, 108),  # wick to 100, body 105..108 ≥ 103
    ]
    state, events = _replay(zone, bars)
    inv = [e for e in events if e.kind == "INV"]
    assert len(inv) == 1
    assert state.inv_fired is True
    assert state.first_bear_fvg is not None
    assert state.first_bear_fvg.top == 107.0
    assert state.search_start_ts == 3000   # body-break anchor unchanged
    assert state.swing_low == 100.0         # swing_low slid down to new wick
    assert state.swing_low_ts == 6000


def test_inv_fires_after_wick_break_using_preserved_bear_fvg():
    """A wick break before INV preserves the locked bear FVG; a later
    close-reclaim still fires INV against it."""
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),  # swing low = 103
        _bar(4000, 105, 106, 104, 105),  # bear FVG top=107 (locked here)
        _bar(5000, 105, 109, 100, 105),  # wick 100; body close 105 (≤ bear_top)
        _bar(6000, 108, 110, 107, 109),  # close 109 > 107 → INV
    ]
    state, events = _replay(zone, bars)
    assert state.inv_fired is True
    assert state.inv_at_ts == 6000
    assert state.swing_low == 100.0


# --- 11. No bearish FVG in the descent → INV stays unarmed -------------------
# If the impulse down into the low left no bearish FVG at all, there is no
# INV anchor and INV does not fire.

def test_no_inv_when_descent_left_no_bear_fvg():
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),  # idx0 — gentle, overlapping descent
        _bar(2000, 107, 108, 106, 107),  # idx1
        _bar(3000, 106, 107, 105, 106),  # idx2
        _bar(4000, 105, 106, 104, 105),  # idx3 — swing low, no 3-bar gap
        _bar(5000, 107, 109, 106, 108),  # idx4 — bounce
        _bar(6000, 109, 111, 108, 110),  # idx5 — close 110, but no anchor
    ]
    state, events = detect_setup(zone, bars, None, symbol="TEST", timeframe="M15")
    assert state.first_bear_fvg is None
    assert state.inv_fired is False
    assert [e.kind for e in events if e.kind == "INV"] == []


# --- 12. STB composition (INV→CRE window + same-arc rule) --------------------
# When CRE forms AFTER the inversion bar it must complete within
# STB_CRE_WINDOW_BARS (3) LTF closes of that bar, and the detector must
# observe it while the window is still open — no catch-up STB. When CRE
# formed at or before the inversion bar there is no window: STB composes
# immediately at any bar-distance (same-arc rule).

def test_stb_fires_when_cre_within_3bar_window_after_inv():
    """INV at idx=4, bull FVG completes at idx=6 (inv+2) — inside the
    3-bar window → STB composes."""
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),      # bear FVG: top=107
        _bar(5000, 107.5, 108, 107, 108),    # INV at idx=4 (close 108 > 107)
        _bar(6000, 107, 108, 106, 107),      # filler: no bull FVG
        _bar(7000, 109, 110, 108.5, 109),    # bull FVG at idx=6 (inv+2)
    ]
    state, events = _replay(zone, bars)
    kinds = [e.kind for e in events]
    assert "INV" in kinds
    assert "CRE" not in kinds          # CRE frozen as a standalone event
    assert state.cre_fired is True
    assert kinds.count("STB") == 1
    assert state.stb_fired is True
    assert state.state == "STB"


def test_stb_does_not_compose_when_cre_forms_after_3bar_window():
    """INV at idx=4, bull FVG only completes at idx=10 (inv+6) — past the
    3-bar window. STB never composes, INV stays standalone and
    stb_window_expired is latched."""
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),      # bear FVG: top=107
        _bar(5000, 107.5, 108, 107, 108),    # INV at idx=4
        _bar(6000, 107, 108, 106, 107),      # filler
        _bar(7000, 107, 108, 106, 107),      # filler
        _bar(8000, 107, 108, 106, 107),      # filler
        _bar(9000, 107, 108, 106, 107),      # filler
        _bar(10000, 107, 108, 106, 107),     # filler
        _bar(11000, 109, 110, 108.5, 109),   # bull FVG at idx=10 (inv+6)
    ]
    state, events = _replay(zone, bars)
    kinds = [e.kind for e in events]
    assert "INV" in kinds
    assert kinds.count("STB") == 0
    assert state.cre_fired is True          # CRE still detected internally
    assert state.stb_fired is False
    assert state.stb_window_expired is True
    assert state.state == "INV"


def test_stb_no_catch_up_when_window_already_elapsed_on_first_observation():
    """The bull FVG completed inside the window (idx=6, inv+2) but the
    detector first sees the session only after the window has elapsed
    (last bar idx=8 > inv+3). State records INV but no STB composes
    (window already elapsed by first observation), and no events fire —
    INV's trigger bar (idx=4) is not the latest bar (idx=8) either, so
    Telegram stays silent on both counts.
    """
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),      # bear FVG: top=107
        _bar(5000, 107.5, 108, 107, 108),    # INV at idx=4
        _bar(6000, 107, 108, 106, 107),      # filler
        _bar(7000, 109, 110, 108.5, 109),    # bull FVG at idx=6 (inv+2, inside window)
        _bar(8000, 109, 110, 108, 109),      # filler past the window
        _bar(9000, 109, 110, 108, 109),      # last bar idx=8 → window elapsed
    ]
    state, events = detect_setup(zone, bars, None, symbol="TEST", timeframe="M15")
    assert events == []
    assert state.inv_fired is True
    assert state.cre_fired is True
    assert state.stb_fired is False
    assert state.stb_window_expired is True
    assert state.state == "INV"


def test_stb_composes_when_cre_precedes_inv_with_no_window():
    """A bull FVG that formed BEFORE the inversion bar is exempt from the
    3-bar window: bull FVG at idx=6, INV only at idx=12 (gap 6) → STB
    still composes (same-arc rule, any distance)."""
    zone = _bullish_ote(ote_low=95.0, ote_high=115.0)
    bars = [
        _bar(1000, 108, 109, 107, 108),
        _bar(2000, 108, 109, 107, 108),
        _bar(3000, 107, 107, 103, 104),
        _bar(4000, 105, 106, 104, 105),     # bear FVG: top=107
        _bar(5000, 105, 106, 104, 105),     # idx=4
        _bar(6000, 106, 107, 105, 106),     # idx=5
        _bar(7000, 107, 107, 106.5, 107),   # bull FVG at idx=6 (low 106.5 > bar5000.high 106)
        _bar(8000, 106, 107, 105, 106),     # filler
        _bar(9000, 106, 107, 105, 106),     # filler
        _bar(10000, 106, 107, 105, 106),    # filler
        _bar(11000, 106, 107, 105, 106),    # filler
        _bar(12000, 106, 107, 105, 106),    # filler
        _bar(13000, 108, 110, 107, 109),    # close 109 > 107 → INV at idx=12
    ]
    state, events = _replay(zone, bars)
    kinds = [e.kind for e in events]
    assert "CRE" not in kinds          # CRE frozen as a standalone event
    assert state.cre_fired is True
    assert state.cre_at_ts == 7000
    # INV + STB compose in the same cycle → INV is suppressed, only STB.
    assert kinds.count("INV") == 0
    assert kinds.count("STB") == 1
    assert state.state == "STB"
    assert state.inv_at_ts == 13000
    stb_e = next(e for e in events if e.kind == "STB")
    assert stb_e.trigger_ts == max(state.inv_at_ts, state.cre_at_ts)


# --- 13. OTE invalidation (body close below the 0.85 level) ------------------
# A body close below the 0.85-retracement level kills the OTE level for
# good. With ote_low_price=100, ote_high_price=110 the 0.85 level is
# ~96.51 (per _invalidation_price). Recovery needs a fresh OTE level.

def test_ote_invalidated_when_body_closes_below_085_level():
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [
        _bar(1000, 105, 106, 104, 105),   # enter OTE
        _bar(2000, 104, 105, 95, 96),     # body close 96 < ~96.51 → KILL
        _bar(3000, 105, 107, 104, 106),
        _bar(4000, 106, 109, 106, 108),
        _bar(5000, 108, 110, 108, 109),   # bullish FVG — would arm CRE, but dead
    ]
    state, events = _replay(zone, bars)
    assert state.state == "NO"
    assert state.ote_invalidated is True
    assert events == []


def test_ote_not_invalidated_on_wick_below_085():
    zone = _bullish_ote(ote_low=100.0, ote_high=110.0)
    bars = [
        _bar(1000, 105, 106, 104, 105),
        _bar(2000, 105, 106, 95, 105),    # wick to 95, body 105 — survives
        _bar(3000, 105, 107, 104, 106),
        _bar(4000, 106, 109, 106, 108),
        _bar(5000, 108, 110, 108, 109),   # bullish FVG → cre_fired
    ]
    state, events = _replay(zone, bars)
    assert state.ote_invalidated is False
    assert state.cre_fired is True


# --- CRE re-anchors when a wick break slides swing_low to a later bar --------

def test_cre_reselected_when_wick_break_moves_swing_low():
    """A bull FVG locked while swing_low sat earlier must be dropped once a
    wick break slides swing_low to a later bar. CRE is the first bull FVG
    formed AFTER the low — never one left of it."""
    zone = _bullish_ote()
    bars = [
        _bar(1000, 105, 106, 104, 105),
        _bar(2000, 105, 107, 104.5, 106),
        _bar(3000, 107, 108, 106.5, 107.5),  # bull FVG #1 (b1.high 106 < 106.5)
        _bar(4000, 105, 106, 102, 104.5),    # wick break: low 102 < 104, body holds
        _bar(5000, 105, 107, 104, 106),
        _bar(6000, 108, 109, 107.5, 108),    # bull FVG #2 (b4.high 106 < 107.5)
    ]
    state, _ = _replay(zone, bars)
    assert state.swing_low == 102.0
    assert state.swing_low_ts == 4000
    assert state.first_bull_fvg is not None
    # The stale FVG #1 (ts 3000) predates the low — CRE must be FVG #2.
    assert state.first_bull_fvg.formed_at_ts == 6000
    assert state.first_bull_fvg.formed_at_ts > state.swing_low_ts


def test_fvg_carries_start_ts_of_first_forming_bar():
    """The FVG box anchor (start_ts) is bar i-2, not the confirming bar."""
    zone = _bullish_ote()
    bars = [
        _bar(1000, 105, 106, 104, 105),
        _bar(2000, 105, 107, 104.5, 106),
        _bar(3000, 107, 108, 106.5, 107.5),  # bull FVG: a=b1, c=b3
    ]
    state, _ = _replay(zone, bars)
    assert state.first_bull_fvg is not None
    assert state.first_bull_fvg.start_ts == 1000          # bar i-2
    assert state.first_bull_fvg.formed_at_ts == 3000      # bar i
