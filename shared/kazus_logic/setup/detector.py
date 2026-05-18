"""
INV/CRE/STB setup detector for the bullish-OTE long-only spec.

Pure function: takes the current HTF zone, all closed LTF bars, and the
previous SetupState; returns the new state plus any events that fired
this cycle.

Spec recap (bullish OTE, HTF retraced down, expecting reversal up):
  INV — bearish FVG formed within BEAR_FVG_LOOKBACK_BARS strictly before
        the session swing_low (inclusive of the swing_low bar); fires when
        an LTF candle later closes its body above that FVG's top. If no
        bear FVG sits inside that window the setup is unarmed until a new
        swing_low appears.
  CRE — first bullish FVG of the reaction off the swing_low, i.e. the
        first bullish FVG whose formation bar lies strictly AFTER the
        current swing_low bar; fires on the formation bar itself (no
        body-reclaim required). FVGs from the descent into the low or
        from later secondary price action are not eligible.
        CRE is currently FROZEN as a standalone setup: `cre_fired` is
        still tracked because it is a required ingredient of STB, but no
        CRE SetupEvent is emitted and the state never settles on "CRE".
        CRE only ever surfaces folded into STB.
  STB — INV and CRE both fired in the same search arc. How the two
        anchors compose depends on the order they formed:
          * CRE formed at or before the inversion bar → STB composes
            immediately, with no window. A body-break of the swing_low
            resets the arc and clears both flags, so two surviving
            flags already prove the market printed no new low between
            the events — one base, one inversion of the same locked
            bearish FVG, any bar-distance allowed (same-arc rule).
          * CRE formed AFTER the inversion bar → its bull FVG must
            fully form within STB_CRE_WINDOW_BARS LTF closes of the
            inversion bar (the closing 3rd bar of the FVG must land in
            inv+1..inv+STB_CRE_WINDOW_BARS). This case is strict on
            LTF-close cadence: a CRE that completes later, or one the
            detector first observes only after the window has already
            elapsed, does NOT compose — no catch-up STB is emitted.
        Once the window has elapsed without CRE, stb_window_expired is
        latched and STB can never form on this arc; a swing-low
        body-break clears it together with the rest of the flags.

We fire on first observation of a closed-bar trigger, not only when the
trigger bar happens to be the latest one in the window: the worker polls
every few minutes while LTF candles close every 15m, so a setup can
already be hours old by the time it is first observed (fresh start,
restart, missed cycle). Once a closed-bar trigger has fired and not been
invalidated, it stays valid and the runner-level sent_event_ids dedup
keeps the same trigger from re-alerting on later cycles. The one
exception is the INV→CRE window for STB (above): it is gated on
LTF-close cadence and never emits a catch-up STB.

Swing-low replay (within a live session):
  Each cycle replays arc bars forward from the bar where the prior
  cycle's swing_low was last anchored. For every newer bar:
    - body close strictly below swing_low (min(open, close) < swing_low)
      → body-break: full reset within the same session. search_start_ts
        advances to the breaking bar, FVG locks and event flags clear,
        a new search arc begins. This is the "stop setup" condition.
    - bar.low strictly below swing_low but body close at-or-above it
      → wick-only break: swing_low moves down to the new wick low.
        Locked FVGs, event flags, and search_start_ts are preserved.

Session boundary:
  A session is tied to one OTE level for its whole lifetime. It begins at
  the first bar that entered the OTE band and lives until the HTF OTE
  bounds themselves change. Price dipping below OTE — even to print the
  swing low — stays in-session and does NOT re-anchor it; price leaving
  and re-entering OTE does not start a new session either. session_id
  encodes the OTE bounds only.

OTE invalidation:
  A body close below the 0.85-retracement level (OTE_INVALIDATION_
  RETRACEMENT — deeper than the OTE band's 0.79 lower edge, above the 1.0
  swing-low anchor) permanently kills the OTE level. From that point the
  detector returns NO for this session_id and does not search the setup
  again, even if price climbs back into the OTE band — a fresh OTE level
  (a different session_id) is required to resume. The kill is re-derived
  from the session bars each cycle, so it holds across the stretch where
  price is out of OTE entirely. The swing_low is still the absolute LTF
  minimum of the session, never re-anchored to a later re-entry bar.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

from ..engine import Bar, OTE_HIGH, OTE_LOW, ZoneResult
from .types import Fvg, SetupEvent, SetupState


# A bearish FVG qualifies as an INV anchor only if its formation bar lies
# within this many bars strictly BEFORE the current swing_low bar (the
# swing_low bar itself is the right edge and is inclusive).
BEAR_FVG_LOOKBACK_BARS = 5

# Once INV has fired, a CRE that forms AFTER the inversion bar has at
# most this many LTF-candle closes to fully form: the bull FVG's closing
# formation bar must land in inv+1..inv+STB_CRE_WINDOW_BARS. A CRE that
# completes later — or one the detector first observes only after the
# window has elapsed — does not compose into STB. A bull FVG formed at or
# before the inversion bar is exempt (same-arc rule, any distance).
STB_CRE_WINDOW_BARS = 3

# A body close below this retracement level permanently kills the OTE
# level: the setup is never searched on it again, even if price climbs
# back into the OTE band. 0.85 sits below the band's lower edge
# (OTE_HIGH) but above the 1.0 swing-low anchor.
OTE_INVALIDATION_RETRACEMENT = 0.85


def detect_setup(
    zone: ZoneResult,
    ltf_bars: List[Bar],
    prev_state: Optional[SetupState],
    *,
    symbol: str = "",
    timeframe: str = "",
) -> Tuple[SetupState, List[SetupEvent]]:
    if not zone.in_ote or zone.direction != "bullish":
        return SetupState(state="NO"), []

    if not ltf_bars:
        return SetupState(state="NO"), []

    ote_low = zone.ote_low_price
    ote_high = zone.ote_high_price
    if ote_low is None or ote_high is None:
        return SetupState(state="NO"), []

    entry_idx = _find_session_start(ltf_bars, ote_low, ote_high)
    if entry_idx is None:
        return SetupState(state="NO"), []

    session_bars = ltf_bars[entry_idx:]
    if not session_bars:
        return SetupState(state="NO"), []

    session_id = _make_session_id(ote_low, ote_high)

    # OTE invalidation. A body close below the 0.85-retracement level kills
    # this OTE level for good — no setup is searched on it again, even if
    # price climbs back into the band. Recovery requires a brand-new OTE
    # level (a different session_id). Re-derived from the session bars
    # every cycle so it survives the stretch where price is out of OTE and
    # this function returns NO without persisting state.
    invalidation_price = _invalidation_price(ote_low, ote_high)
    invalidated = (
        prev_state is not None
        and prev_state.session_id == session_id
        and prev_state.ote_invalidated
    )
    if not invalidated and invalidation_price is not None:
        invalidated = any(
            min(b.open, b.close) < invalidation_price for b in session_bars
        )
    if invalidated:
        return SetupState(
            state="NO", session_id=session_id, ote_invalidated=True
        ), []

    if prev_state is None or prev_state.session_id != session_id:
        state = SetupState(
            session_id=session_id,
            search_start_ts=session_bars[0].ts,
        )
    else:
        state = _clone_state(prev_state)

    _apply_swing_low_events(state, session_bars)

    last_bar = session_bars[-1]

    all_fvgs = _scan_fvgs_in_session(session_bars)
    eligible_fvgs = [f for f in all_fvgs if f.formed_at_ts >= state.search_start_ts]

    swing_low_idx = _idx_of_ts(session_bars, state.swing_low_ts)

    # Bear FVG selection — must lie inside the lookback window ending at
    # the current swing_low bar. Once locked, the bear FVG is kept across
    # wick-only swing_low updates (which can shift the window away from
    # the FVG). Only a body-break clears it via the full state reset.
    if state.first_bear_fvg is None and swing_low_idx is not None:
        lo_idx = max(0, swing_low_idx - BEAR_FVG_LOOKBACK_BARS)
        state.first_bear_fvg = next(
            (
                f
                for f in eligible_fvgs
                if f.kind == "bearish"
                and lo_idx <= f.formed_at_idx <= swing_low_idx
            ),
            None,
        )

    # Bull FVG (CRE) selection — must be the FIRST bullish FVG of the
    # reaction OFF the swing_low, i.e. formed strictly after the swing_low
    # bar. Without this anchor the detector would lock any earlier bullish
    # FVG from the descent into the low, or skip the genuine first one and
    # pick a later local FVG from secondary price action. Once locked the
    # FVG is kept across wick-only swing_low updates; a body-break clears
    # it via the full state reset.
    if state.first_bull_fvg is None and swing_low_idx is not None:
        state.first_bull_fvg = next(
            (
                f
                for f in eligible_fvgs
                if f.kind == "bullish" and f.formed_at_idx > swing_low_idx
            ),
            None,
        )

    events: List[SetupEvent] = []
    swing_low_for_event = state.swing_low if state.swing_low is not None else last_bar.low

    # INV trigger: first session bar (after the bear FVG formed) whose body
    # closes strictly above the FVG's top. Scanning the whole arc (not only
    # last_bar) means a still-valid trigger that closed before the worker
    # observed the session is not silently dropped.
    if state.first_bear_fvg is not None and not state.inv_fired:
        bear_top = state.first_bear_fvg.top
        bear_formed_ts = state.first_bear_fvg.formed_at_ts
        for b in session_bars:
            if b.ts < state.search_start_ts or b.ts <= bear_formed_ts:
                continue
            body_low = min(b.open, b.close)
            if body_low > bear_top:
                state.inv_fired = True
                state.inv_at_ts = b.ts
                events.append(SetupEvent(
                    kind="INV",
                    event_id=_event_id("inv", symbol, timeframe, b.ts),
                    trigger_ts=b.ts,
                    fvg=state.first_bear_fvg,
                    swing_low=swing_low_for_event,
                ))
                break

    # CRE detection: formation of the first bullish FVG. Once first_bull_fvg
    # is locked in for this arc the formation bar is fixed; recording it on
    # first observation captures it across worker restarts and missed
    # cycles. CRE is FROZEN as a standalone setup — `cre_fired` is tracked
    # purely so STB can compose; no CRE SetupEvent is emitted here.
    if state.first_bull_fvg is not None and not state.cre_fired:
        state.cre_fired = True
        state.cre_at_ts = state.first_bull_fvg.formed_at_ts

    # STB composition. INV and CRE must both have fired in this arc; how
    # they compose depends on the order their anchor bars formed:
    #   * bull FVG at or before the inversion bar → same-arc rule, STB
    #     composes immediately with no window. A body-break would have
    #     reset both flags, so two surviving flags prove one continuous
    #     base — any bar-distance is allowed.
    #   * bull FVG after the inversion bar → it must fully form within
    #     STB_CRE_WINDOW_BARS LTF closes of the inversion bar, AND the
    #     detector must observe it while the window is still open
    #     (last closed bar no later than inv+STB_CRE_WINDOW_BARS). A CRE
    #     completing later, or first seen after the window elapsed, does
    #     not compose — no catch-up STB.
    if (
        state.inv_fired
        and state.cre_fired
        and not state.stb_fired
        and not state.stb_window_expired
    ):
        inv_idx = _idx_of_ts(session_bars, state.inv_at_ts)
        bull_idx = (
            _idx_of_ts(session_bars, state.first_bull_fvg.formed_at_ts)
            if state.first_bull_fvg is not None else None
        )
        last_idx = len(session_bars) - 1
        compose = False
        if inv_idx is not None and bull_idx is not None:
            if bull_idx <= inv_idx:
                compose = True
            elif (
                bull_idx <= inv_idx + STB_CRE_WINDOW_BARS
                and last_idx <= inv_idx + STB_CRE_WINDOW_BARS
            ):
                compose = True
        if compose:
            state.stb_fired = True
            stb_ts = max(state.inv_at_ts or 0, state.cre_at_ts or 0)
            stb_fvg = state.first_bull_fvg or state.first_bear_fvg
            assert stb_fvg is not None
            events.append(SetupEvent(
                kind="STB",
                event_id=_event_id("stb", symbol, timeframe, stb_ts),
                trigger_ts=stb_ts,
                fvg=stb_fvg,
                swing_low=swing_low_for_event,
            ))

    # Window expiry. INV has fired but no STB composed and the LTF has
    # closed past inv+STB_CRE_WINDOW_BARS — the INV→CRE window is over for
    # good. Latch stb_window_expired so a CRE that only appears (or is
    # only observed) later can never compose into STB on this arc. A
    # swing-low body-break clears the latch with the rest of the flags.
    if (
        state.inv_fired
        and not state.stb_fired
        and not state.stb_window_expired
    ):
        inv_idx = _idx_of_ts(session_bars, state.inv_at_ts)
        last_idx = len(session_bars) - 1
        if inv_idx is not None and last_idx > inv_idx + STB_CRE_WINDOW_BARS:
            state.stb_window_expired = True

    # CRE is frozen as a standalone state — a bullish FVG without INV
    # produces no setup. Only NO / INV / STB are ever surfaced.
    if state.stb_fired:
        state.state = "STB"
    elif state.inv_fired:
        state.state = "INV"
    else:
        state.state = "NO"

    # INV + STB composing in the SAME cycle would otherwise fire two
    # near-identical alerts. STB supersedes INV — drop the INV event so
    # only the STB alert goes out. An INV emitted in an earlier cycle was
    # already sent standalone and is unaffected (events is per-call).
    if any(e.kind == "STB" for e in events):
        events = [e for e in events if e.kind != "INV"]

    return state, events


def _find_session_start(
    bars: List[Bar], ote_low: float, ote_high: float
) -> Optional[int]:
    """Return the index of the bar where the current OTE session began.

    The session is tied to one OTE level for its whole lifetime, so the
    start is the FIRST bar in the window that entered the OTE band. Every
    bar after it belongs to the session, including bars that later dipped
    below OTE to print the swing low — those do NOT re-anchor the start.
    Scanning back from the end (the old behaviour) wrongly re-anchored the
    session to a recent OTE re-touch during the reaction, which dropped
    the real swing low from the session window.

    Returns None if no bar in the window ever touched the OTE band.
    """
    for i, b in enumerate(bars):
        if b.low <= ote_high and b.high >= ote_low:
            return i
    return None


def _apply_swing_low_events(state: SetupState, session_bars: List[Bar]) -> None:
    """Replay arc bars after the current swing_low anchor and mutate state
    in place. Two break classes:
      - body close < swing_low → full reset within the same session.
      - bar.low < swing_low, body close ≥ swing_low → swing_low moves
        to the new wick low; FVG locks and event flags are preserved.
    Fresh state (swing_low None) seeds swing_low from the first arc bar
    and then replays forward from the next bar.
    """
    arc = [b for b in session_bars if b.ts >= state.search_start_ts]
    if not arc:
        return

    if state.swing_low is None:
        state.swing_low = arc[0].low
        state.swing_low_ts = arc[0].ts
        i = 1
    else:
        anchor_ts = (
            state.swing_low_ts
            if state.swing_low_ts is not None
            else state.search_start_ts
        )
        i = len(arc)
        for j, b in enumerate(arc):
            if b.ts > anchor_ts:
                i = j
                break

    while i < len(arc):
        b = arc[i]
        body_low = min(b.open, b.close)
        if body_low < state.swing_low:
            state.search_start_ts = b.ts
            state.swing_low = b.low
            state.swing_low_ts = b.ts
            state.first_bear_fvg = None
            state.first_bull_fvg = None
            state.inv_fired = False
            state.cre_fired = False
            state.stb_fired = False
            state.inv_at_ts = None
            state.cre_at_ts = None
            state.stb_window_expired = False
            arc = [bb for bb in session_bars if bb.ts >= state.search_start_ts]
            i = 1
        elif b.low < state.swing_low:
            state.swing_low = b.low
            state.swing_low_ts = b.ts
            # The reaction OFF the low restarts at the new (later) low, so
            # the CRE anchor — the first bull FVG formed AFTER the low —
            # must be re-selected. Keeping the old lock leaves a CRE that
            # predates the low (the FVG drawn to the left of the swing).
            # The bear FVG / INV are intentionally sticky (lookback rule).
            if not state.stb_fired:
                state.first_bull_fvg = None
                state.cre_fired = False
                state.cre_at_ts = None
            i += 1
        else:
            i += 1


def _scan_fvgs_in_session(bars: List[Bar]) -> List[Fvg]:
    """3-bar FVGs in chronological order. The gap is anchored at bar i,
    measured against bar i-2 (rule of three).
      bullish:  bars[i].low > bars[i-2].high   → top=bars[i].low, bottom=bars[i-2].high
      bearish:  bars[i].high < bars[i-2].low   → top=bars[i-2].low, bottom=bars[i].high
    """
    out: List[Fvg] = []
    for i in range(2, len(bars)):
        a = bars[i - 2]
        c = bars[i]
        if c.low > a.high:
            out.append(Fvg(
                formed_at_idx=i,
                formed_at_ts=c.ts,
                top=c.low,
                bottom=a.high,
                kind="bullish",
                start_ts=a.ts,
            ))
        elif c.high < a.low:
            out.append(Fvg(
                formed_at_idx=i,
                formed_at_ts=c.ts,
                top=a.low,
                bottom=c.high,
                kind="bearish",
                start_ts=a.ts,
            ))
    return out


def _idx_of_ts(bars: List[Bar], ts: Optional[int]) -> Optional[int]:
    if ts is None:
        return None
    for i, b in enumerate(bars):
        if b.ts == ts:
            return i
    return None


def _invalidation_price(
    ote_low_price: float, ote_high_price: float
) -> Optional[float]:
    """Price of the OTE_INVALIDATION_RETRACEMENT (0.85) level.

    Derived from the two OTE-band prices: ote_high_price is the OTE_LOW
    (0.618) level, ote_low_price the OTE_HIGH (0.79) level. The fib is
    linear in retracement, so the per-retracement-unit price span — and
    the deeper 0.85 level below the band — follow directly. Returns None
    if the band is degenerate.
    """
    band = ote_high_price - ote_low_price
    span = OTE_HIGH - OTE_LOW
    if band <= 0 or span <= 0:
        return None
    per_unit = band / span
    return ote_low_price - (OTE_INVALIDATION_RETRACEMENT - OTE_HIGH) * per_unit


def _make_session_id(ote_low: float, ote_high: float) -> str:
    # Keyed on the OTE bounds only — the session lives as long as the OTE
    # level does. Entry ts is deliberately excluded so price leaving and
    # re-entering OTE cannot spawn a fresh session (and reseed swing_low).
    raw = f"{ote_low:.10g}|{ote_high:.10g}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _event_id(kind: str, symbol: str, timeframe: str, ts: int) -> str:
    prefix = f"{symbol}:{timeframe}" if (symbol or timeframe) else ""
    return f"{kind}:{prefix}:{ts}" if prefix else f"{kind}:{ts}"


def _clone_state(s: SetupState) -> SetupState:
    return SetupState(
        state=s.state,
        session_id=s.session_id,
        search_start_ts=s.search_start_ts,
        swing_low=s.swing_low,
        swing_low_ts=s.swing_low_ts,
        first_bear_fvg=s.first_bear_fvg,
        first_bull_fvg=s.first_bull_fvg,
        inv_fired=s.inv_fired,
        cre_fired=s.cre_fired,
        stb_fired=s.stb_fired,
        inv_at_ts=s.inv_at_ts,
        cre_at_ts=s.cre_at_ts,
        ote_invalidated=s.ote_invalidated,
        stb_window_expired=s.stb_window_expired,
    )
