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
  STB — INV and CRE both fired AND their anchor bars are within
        STB_WINDOW_BARS of each other (symmetric, no order required).
        If the gap is wider than the window the standalone INV and CRE
        fire as separate events but STB does not.

We fire on first observation of a closed-bar trigger, not only when the
trigger bar happens to be the latest one in the window: the worker polls
every few minutes while LTF candles close every 15m, so a setup can
already be hours old by the time it is first observed (fresh start,
restart, missed cycle). Once a closed-bar trigger has fired and not been
invalidated, it stays valid and the runner-level sent_event_ids dedup
keeps the same trigger from re-alerting on later cycles.

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
  A new session starts when the HTF OTE bounds change OR price had left
  OTE and re-entered. session_id encodes both.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

from ..engine import Bar, ZoneResult
from .types import Fvg, SetupEvent, SetupState


# A bearish FVG qualifies as an INV anchor only if its formation bar lies
# within this many bars strictly BEFORE the current swing_low bar (the
# swing_low bar itself is the right edge and is inclusive).
BEAR_FVG_LOOKBACK_BARS = 5

# INV and CRE compose into STB only if their anchor bars are within this
# many bars of each other (symmetric — CRE may precede or follow INV).
STB_WINDOW_BARS = 5


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

    # STB only composes if INV's body-close bar and CRE's bull-FVG bar are
    # within STB_WINDOW_BARS of each other (symmetric). Outside the window
    # the two events stand alone — INV and CRE are still emitted but no
    # STB is produced.
    if state.inv_fired and state.cre_fired and not state.stb_fired:
        inv_idx = _idx_of_ts(session_bars, state.inv_at_ts)
        bull_idx = (
            _idx_of_ts(session_bars, state.first_bull_fvg.formed_at_ts)
            if state.first_bull_fvg is not None else None
        )
        if (
            inv_idx is not None
            and bull_idx is not None
            and abs(bull_idx - inv_idx) <= STB_WINDOW_BARS
        ):
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
    elif state.cre_fired:
        state.state = "CRE"
    elif state.inv_fired:
        state.state = "INV"
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
            arc = [bb for bb in session_bars if bb.ts >= state.search_start_ts]
            i = 1
        elif b.low < state.swing_low:
            state.swing_low = b.low
            state.swing_low_ts = b.ts
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


def _idx_of_ts(bars: List[Bar], ts: Optional[int]) -> Optional[int]:
    if ts is None:
        return None
    for i, b in enumerate(bars):
        if b.ts == ts:
            return i
    return None


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
