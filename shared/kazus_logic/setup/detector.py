"""
INV/CRE/STB setup detector for the bullish-OTE long-only spec.

Pure function: takes the current HTF zone, all closed LTF bars, and the
previous SetupState; returns the new state plus any events that fired
this cycle.

Spec recap (bullish OTE, HTF retraced down, expecting reversal up):
  INV — first bearish FVG formed AFTER price enters OTE; fires when an
        LTF candle closes its body above that FVG's top.
  CRE — first bullish FVG formed AFTER price enters OTE; fires on the
        formation bar itself (no body-reclaim required).
  STB — INV and CRE both fired within the same OTE session, in any order.
        Trigger ts is the later of the two.

We fire on first observation of a closed-bar trigger, not only when the
trigger bar happens to be the latest one in the window: the worker polls
every few minutes while LTF candles close every 15m, so a setup can
already be hours old by the time it is first observed (fresh start,
restart, missed cycle). Once a closed-bar trigger has fired and not been
invalidated, it stays valid and the runner-level sent_event_ids dedup
keeps the same trigger from re-alerting on later cycles.

Reset (within a live session):
  If the last closed LTF bar's low breaks the session swing_low (the
  minimum that "spawned" the search), all event flags wipe and we
  re-arm from a fresh swing_low. Already-emitted events are not recalled.

Session boundary:
  A new session starts when the HTF OTE bounds change OR price had left
  OTE and re-entered. session_id encodes both.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

from ..engine import Bar, ZoneResult
from .types import Fvg, SetupEvent, SetupState


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

    session_id = _make_session_id(ote_low, ote_high, session_bars[0].ts)

    if prev_state is None or prev_state.session_id != session_id:
        state = SetupState(
            session_id=session_id,
            search_start_ts=session_bars[0].ts,
        )
    else:
        state = _clone_state(prev_state)

    last_bar = session_bars[-1]

    swing_was_broken = (
        state.swing_low is not None
        and last_bar.low < state.swing_low
    )
    if swing_was_broken:
        state = SetupState(
            session_id=session_id,
            search_start_ts=last_bar.ts,
            swing_low=last_bar.low,
            swing_low_ts=last_bar.ts,
        )
    else:
        # Track minimum within the current search arc (since last reset).
        arc_bars = [b for b in session_bars if b.ts >= state.search_start_ts]
        cur_swing_low, cur_swing_low_ts = _session_swing_low(arc_bars)
        state.swing_low = cur_swing_low
        state.swing_low_ts = cur_swing_low_ts

    fvgs = _scan_fvgs_in_session(session_bars)
    eligible_fvgs = [f for f in fvgs if f.formed_at_ts >= state.search_start_ts]
    if state.first_bear_fvg is None:
        state.first_bear_fvg = next(
            (f for f in eligible_fvgs if f.kind == "bearish"), None
        )
    if state.first_bull_fvg is None:
        state.first_bull_fvg = next(
            (f for f in eligible_fvgs if f.kind == "bullish"), None
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

    # CRE trigger: formation of the first bullish FVG. Once first_bull_fvg
    # is locked in for this arc the formation bar is fixed; firing on first
    # observation captures it across worker restarts and missed cycles.
    if state.first_bull_fvg is not None and not state.cre_fired:
        state.cre_fired = True
        state.cre_at_ts = state.first_bull_fvg.formed_at_ts
        events.append(SetupEvent(
            kind="CRE",
            event_id=_event_id("cre", symbol, timeframe, state.cre_at_ts),
            trigger_ts=state.cre_at_ts,
            fvg=state.first_bull_fvg,
            swing_low=swing_low_for_event,
        ))

    if state.inv_fired and state.cre_fired and not state.stb_fired:
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

    if state.stb_fired:
        state.state = "STB"
    elif state.inv_fired:
        state.state = "INV"
    elif state.cre_fired:
        state.state = "CRE"
    else:
        state.state = "NO"

    return state, events


def _find_session_start(
    bars: List[Bar], ote_low: float, ote_high: float
) -> Optional[int]:
    """Return the index of the bar where the current OTE-session began.

    Walks back from the end of `bars`, finds the most recent bar that was
    fully outside the OTE band (below or above), and returns the index
    immediately after it. If the entire window is inside OTE, returns 0.
    Returns None if no bar in the window touches OTE.
    """
    last_in_ote = -1
    for i in range(len(bars) - 1, -1, -1):
        b = bars[i]
        if b.low <= ote_high and b.high >= ote_low:
            last_in_ote = i
        else:
            break
    if last_in_ote == -1:
        return None
    return last_in_ote


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
            ))
        elif c.high < a.low:
            out.append(Fvg(
                formed_at_idx=i,
                formed_at_ts=c.ts,
                top=a.low,
                bottom=c.high,
                kind="bearish",
            ))
    return out


def _session_swing_low(bars: List[Bar]) -> Tuple[float, int]:
    lo = bars[0].low
    lo_ts = bars[0].ts
    for b in bars[1:]:
        if b.low < lo:
            lo = b.low
            lo_ts = b.ts
    return lo, lo_ts


def _make_session_id(ote_low: float, ote_high: float, entry_ts: int) -> str:
    raw = f"{ote_low:.10g}|{ote_high:.10g}|{entry_ts}"
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
    )
