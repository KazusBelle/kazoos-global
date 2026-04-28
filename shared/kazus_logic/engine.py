"""
Bar-by-bar implementation of Kazus Global (D1) and Kazus Local (H1) logic,
ported line-by-line from `docs/pine/kazus_global_v3.pine` and
`docs/pine/kazus_local_beta.pine`. Those files are the source of truth —
if behavior diverges, Pine wins.

Two engines are exposed:

- KazusGlobalEngine — port of kazus_global_v3.pine. Engulfing signal →
  internal shift → MS detection (close > localHigh / < localLow) → swing
  classification (HH / HL / LL / LH). Fib anchors are
  ta.highestSince(bullishMS, high) / ta.lowestSince(bearishMS, low) and
  the most recent swing low / high.

- KazusLocalEngine — port of kazus_local_beta.pine. Classic zigzag with
  length N. Trend flips when the bar's high makes the N-bar high (uptrend)
  or low makes the N-bar low (downtrend). Pivots are placed at the lowest /
  highest bar in the just-completed leg (lookback = barssince(to_up[1])
  resp. to_down[1]).

Both engines also expose:
- `structure_events`: list of (bar_index, price, label) for the chart UI
- `fvg_events`: list of (bar_index, top, bottom, kind) — bullish: low > high[2],
  bearish: high < low[2]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Zone classification (shared between both engines)
# ---------------------------------------------------------------------------

# User-stated zoning:
# - premium: below 0.5 retracement
# - discount: 0.5 .. 0.618
# - OTE: 0.618 .. 0.79
# - ABNORMAL: above 0.79
PREMIUM_LIMIT = 0.5
OTE_LOW = 0.618
OTE_HIGH = 0.79


@dataclass
class ZoneResult:
    zone: str                 # "premium" | "discount" | "ote" | "abnormal" | "none"
    in_ote: bool
    setup: str                # "yes" | "no"
    retracement: Optional[float]
    direction: str            # "bullish" | "bearish" | "none"
    fib_low: Optional[float]
    fib_high: Optional[float]
    ote_low_price: Optional[float]
    ote_high_price: Optional[float]


def classify_zone(retracement: Optional[float]) -> str:
    if retracement is None:
        return "none"
    if retracement < PREMIUM_LIMIT:
        return "premium"
    if retracement < OTE_LOW:
        return "discount"
    if retracement <= OTE_HIGH:
        return "ote"
    return "abnormal"


def detect_setup(retracement: Optional[float]) -> Tuple[bool, str]:
    in_ote = retracement is not None and OTE_LOW <= retracement <= OTE_HIGH
    return in_ote, "yes" if in_ote else "no"


# ---------------------------------------------------------------------------
# Common data types
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    ts: int           # ms epoch of bar open
    open: float
    high: float
    low: float
    close: float


@dataclass
class FibState:
    direction: str = "none"
    swing_low: Optional[float] = None     # bullish 1.0 anchor
    swing_high: Optional[float] = None    # bearish 1.0 anchor
    fib_low: Optional[float] = None       # bearish 0.0 anchor
    fib_high: Optional[float] = None      # bullish 0.0 anchor
    last_ms_bar: Optional[int] = None

    def active_anchors(self) -> Optional[Tuple[float, float]]:
        if self.direction == "bullish":
            if self.fib_high is None or self.swing_low is None:
                return None
            return self.fib_high, self.swing_low
        if self.direction == "bearish":
            if self.fib_low is None or self.swing_high is None:
                return None
            return self.fib_low, self.swing_high
        return None

    def retracement_for(self, price: float) -> Optional[float]:
        anchors = self.active_anchors()
        if anchors is None:
            return None
        fib0, fib100 = anchors
        if fib100 == fib0:
            return None
        return (price - fib0) / (fib100 - fib0)


# ---------------------------------------------------------------------------
# Kazus Global (D1) — engulfing + market structure
# ---------------------------------------------------------------------------


class KazusGlobalEngine:
    """
    Port of kazus_global_v3.pine.
    """

    def __init__(self) -> None:
        self.bars: List[Bar] = []

        # engulfing signal state
        self.last_signal: int = 0
        self.running_lowest_high: Optional[float] = None
        self.running_highest_low: Optional[float] = None

        # internal-shift bars
        self.last_bull_index: Optional[int] = None
        self.last_bear_index: Optional[int] = None
        self.last_internal_shift: int = 0  # 0 / 1 / -1

        # local-search results from internal shifts (persistent vars in Pine)
        self.local_high: Optional[float] = None
        self.local_high_index: Optional[int] = None
        self.local_low: Optional[float] = None
        self.local_low_index: Optional[int] = None

        # MS state
        self.last_ms_type: Optional[int] = None  # 1 / -1
        self.last_bullish_bos_bar: Optional[int] = None
        self.last_bearish_bos_bar: Optional[int] = None

        # swings
        self.prev_swing_high: Optional[float] = None
        self.prev_swing_low: Optional[float] = None
        self.last_swing_high: Optional[float] = None
        self.last_swing_low: Optional[float] = None
        self.last_high_index: Optional[int] = None
        self.last_low_index: Optional[int] = None

        # tracked fib anchors (running max since bullishMS / running min since bearishMS)
        self.tracked_fib_high: Optional[float] = None
        self.tracked_fib_high_index: Optional[int] = None
        self.tracked_fib_low: Optional[float] = None
        self.tracked_fib_low_index: Optional[int] = None

        # Pending labels (HH? / HL?), confirmed on the next opposite MS event.
        # Stored as the index in self.structure_events of the pending entry,
        # so we can mutate the label in place when it gets confirmed.
        self._pending_hh_event_idx: Optional[int] = None
        self._pending_hl_event_idx: Optional[int] = None

        self.fib_state = FibState()

        # event timeline for chart visualisation
        self.last_structure_event: Optional[str] = None
        # (bar_index, price, label) — label ∈ {HH, HL, LL, LH}
        self.structure_events: List[Tuple[int, float, str]] = []
        # (bar_index, top_price, bottom_price, kind) — kind ∈ {bullish, bearish}
        self.fvg_events: List[Tuple[int, float, float, str]] = []

    # -- main feed ----------------------------------------------------------

    def feed(self, bar: Bar) -> None:
        self.bars.append(bar)
        i = len(self.bars) - 1

        # --- Engulfing detection ----------------------------------------
        starter_bull = False
        starter_bear = False
        if i >= 1:
            prev = self.bars[i - 1]
            starter_bull = (
                prev.close < prev.open
                and bar.close > bar.open
                and bar.close > prev.high
            )
            starter_bear = (
                prev.close > prev.open
                and bar.close < bar.open
                and bar.close < prev.low
            )

        if self.last_signal == 0:
            if starter_bull:
                self.last_signal = 1
                self.running_highest_low = bar.low
            elif starter_bear:
                self.last_signal = -1
                self.running_lowest_high = bar.high

        if self.last_signal == -1:
            self.running_lowest_high = (
                bar.high if self.running_lowest_high is None
                else min(self.running_lowest_high, bar.high)
            )
        elif self.last_signal == 1:
            self.running_highest_low = (
                bar.low if self.running_highest_low is None
                else max(self.running_highest_low, bar.low)
            )

        new_bull = (
            self.last_signal == -1
            and self.running_lowest_high is not None
            and bar.close > self.running_lowest_high
        )
        new_bear = (
            self.last_signal == 1
            and self.running_highest_low is not None
            and bar.close < self.running_highest_low
        )

        if new_bull:
            self.last_bull_index = i
        if new_bear:
            self.last_bear_index = i

        if new_bull:
            self.last_signal = 1
            self.running_lowest_high = None
            self.running_highest_low = bar.low
        elif new_bear:
            self.last_signal = -1
            self.running_highest_low = None
            self.running_lowest_high = bar.high

        # --- Internal-shift gating -------------------------------------
        plot_bear_shift = new_bear and self.last_internal_shift != -1
        plot_bull_shift = new_bull and self.last_internal_shift != 1
        if plot_bear_shift:
            self.last_internal_shift = -1
        if plot_bull_shift:
            self.last_internal_shift = 1

        # --- Local high / low search (only on shift, persists thereafter) ---
        max_history = 10000

        if plot_bear_shift:
            start_idx = max(
                self.last_bull_index if self.last_bull_index is not None else 0,
                i - max_history,
            )
            highest, highest_idx = None, None
            for k in range(start_idx, i + 1):
                if highest is None or self.bars[k].high > highest:
                    highest = self.bars[k].high
                    highest_idx = k
            self.local_high = highest
            self.local_high_index = highest_idx

        if plot_bull_shift:
            start_idx = max(
                self.last_bear_index if self.last_bear_index is not None else 0,
                i - max_history,
            )
            lowest, lowest_idx = None, None
            for k in range(start_idx, i + 1):
                if lowest is None or self.bars[k].low < lowest:
                    lowest = self.bars[k].low
                    lowest_idx = k
            self.local_low = lowest
            self.local_low_index = lowest_idx

        # --- MS detection ----------------------------------------------
        raw_bullish_ms = self.local_high is not None and bar.close > self.local_high
        raw_bearish_ms = self.local_low is not None and bar.close < self.local_low
        can_bullish_ms = self.last_ms_type is None or self.last_ms_type == -1
        can_bearish_ms = self.last_ms_type is None or self.last_ms_type == 1
        bullish_ms = raw_bullish_ms and can_bullish_ms
        bearish_ms = raw_bearish_ms and can_bearish_ms

        if bullish_ms:
            self.last_ms_type = 1
            self.last_bullish_bos_bar = i
        if bearish_ms:
            self.last_ms_type = -1
            self.last_bearish_bos_bar = i

        # --- MS-local extremes (between this MS and the previous opposite MS) ---
        ms_max_history = 5000

        ms_local_low: Optional[float] = None
        ms_local_low_index: Optional[int] = None
        if bullish_ms:
            start = (
                self.last_bearish_bos_bar
                if self.last_bearish_bos_bar is not None
                else max(0, i - ms_max_history)
            )
            for k in range(start, i + 1):
                lo = self.bars[k].low
                if ms_local_low is None or lo < ms_local_low:
                    ms_local_low = lo
                    ms_local_low_index = k

        ms_local_high: Optional[float] = None
        ms_local_high_index: Optional[int] = None
        if bearish_ms:
            start = (
                self.last_bullish_bos_bar
                if self.last_bullish_bos_bar is not None
                else max(0, i - ms_max_history)
            )
            for k in range(start, i + 1):
                h = self.bars[k].high
                if ms_local_high is None or h > ms_local_high:
                    ms_local_high = h
                    ms_local_high_index = k

        # --- Bullish MS: classify HL? / LL ----------------------------
        if bullish_ms and ms_local_low is not None and ms_local_low_index is not None:
            # Pine: any pending HL? on this side — clear it (will be re-issued below if HL).
            if self._pending_hl_event_idx is not None:
                self._pending_hl_event_idx = None

            is_ll = self.prev_swing_low is not None and ms_local_low < self.prev_swing_low
            is_hl = self.prev_swing_low is not None and ms_local_low > self.prev_swing_low

            if is_ll:
                self.last_structure_event = "LL"
                self.structure_events.append((ms_local_low_index, ms_local_low, "LL"))
            elif is_hl:
                self.last_structure_event = "HL"
                self.structure_events.append((ms_local_low_index, ms_local_low, "HL"))
                self._pending_hl_event_idx = len(self.structure_events) - 1
                # Confirm a pending HH? if any (Pine: drop "HH?", commit "HH").
                if self._pending_hh_event_idx is not None:
                    idx = self._pending_hh_event_idx
                    if 0 <= idx < len(self.structure_events):
                        bi, pr, _ = self.structure_events[idx]
                        self.structure_events[idx] = (bi, pr, "HH")
                    self._pending_hh_event_idx = None
            elif self.prev_swing_low is None:
                # First swing — no comparison possible. Pine still records it
                # via the trend machinery but emits no isHH/isHL alert. We treat
                # it as HL? pending so it shows in the chart timeline.
                self.structure_events.append((ms_local_low_index, ms_local_low, "HL"))
                self._pending_hl_event_idx = len(self.structure_events) - 1

            self.prev_swing_low = ms_local_low
            self.last_swing_low = ms_local_low
            self.last_low_index = ms_local_low_index

            # Reset trackedFibHigh on bullishMS.
            self.tracked_fib_high = bar.high
            self.tracked_fib_high_index = i

        # --- Bearish MS: classify HH? / LH ----------------------------
        if bearish_ms and ms_local_high is not None and ms_local_high_index is not None:
            if self._pending_hh_event_idx is not None:
                self._pending_hh_event_idx = None

            is_hh = self.prev_swing_high is not None and ms_local_high > self.prev_swing_high
            is_lh = self.prev_swing_high is not None and ms_local_high < self.prev_swing_high

            if is_hh:
                self.last_structure_event = "HH"
                self.structure_events.append((ms_local_high_index, ms_local_high, "HH"))
                self._pending_hh_event_idx = len(self.structure_events) - 1
                # Confirm a pending HL? if any.
                if self._pending_hl_event_idx is not None:
                    idx = self._pending_hl_event_idx
                    if 0 <= idx < len(self.structure_events):
                        bi, pr, _ = self.structure_events[idx]
                        self.structure_events[idx] = (bi, pr, "HL")
                    self._pending_hl_event_idx = None
            elif is_lh:
                self.last_structure_event = "LH"
                self.structure_events.append((ms_local_high_index, ms_local_high, "LH"))
            elif self.prev_swing_high is None:
                self.structure_events.append((ms_local_high_index, ms_local_high, "HH"))
                self._pending_hh_event_idx = len(self.structure_events) - 1

            self.prev_swing_high = ms_local_high
            self.last_swing_high = ms_local_high
            self.last_high_index = ms_local_high_index

            self.tracked_fib_low = bar.low
            self.tracked_fib_low_index = i

        # --- Update tracked anchors every bar ---------------------------
        if self.tracked_fib_high is None or bar.high >= self.tracked_fib_high:
            self.tracked_fib_high = bar.high
            self.tracked_fib_high_index = i
        if self.tracked_fib_low is None or bar.low <= self.tracked_fib_low:
            self.tracked_fib_low = bar.low
            self.tracked_fib_low_index = i

        # --- FVG (3-bar gap) -------------------------------------------
        if i >= 2:
            two_back = self.bars[i - 2]
            if bar.low > two_back.high:
                self.fvg_events.append((i - 2, bar.low, two_back.high, "bullish"))
            if bar.high < two_back.low:
                self.fvg_events.append((i - 2, two_back.low, bar.high, "bearish"))

        # --- Compose fib_state from latest MS direction ----------------
        # Bullish fib: 0.0 = highestSince(bullishMS, high) = tracked_fib_high
        #              1.0 = lastSwingLow
        # Bearish fib: 0.0 = lowestSince(bearishMS, low) = tracked_fib_low
        #              1.0 = lastSwingHigh
        if self.last_ms_type == 1 and self.last_swing_low is not None and self.tracked_fib_high is not None:
            self.fib_state = FibState(
                direction="bullish",
                swing_low=self.last_swing_low,
                fib_high=self.tracked_fib_high,
                last_ms_bar=self.last_bullish_bos_bar,
            )
        elif self.last_ms_type == -1 and self.last_swing_high is not None and self.tracked_fib_low is not None:
            self.fib_state = FibState(
                direction="bearish",
                swing_high=self.last_swing_high,
                fib_low=self.tracked_fib_low,
                last_ms_bar=self.last_bearish_bos_bar,
            )
        else:
            self.fib_state = FibState()

    # -- snapshot -----------------------------------------------------------

    def snapshot(self, price: float) -> ZoneResult:
        return _zone_result(self.fib_state, price)


# ---------------------------------------------------------------------------
# Kazus Local (H1) — zigzag-based
# ---------------------------------------------------------------------------


class KazusLocalEngine:
    """
    Port of kazus_local_beta.pine.

    Pine semantics that drove the rewrite:

    - to_up   = high >= ta.highest(N)         (N includes current bar)
    - to_down = low  <= ta.lowest(N)
    - trend flips:  trend == 1 and to_down → -1; trend == -1 and to_up → 1
    - last_trend_up_since   = ta.barssince(to_up[1])    (to_up shifted by 1)
                            ≡ i - last_to_up_bar - 1   (or 0 when never seen)
    - low_val   = ta.lowest(nz(last_trend_up_since>0 ? last_trend_up_since : 1))
                ≡ lowest low in the (last_to_up_bar, i] window
    - low_index = bar_index - ta.barssince(low_val == low)
                ≡ the most recent bar in that window whose low equals low_val
    - On trend flip to up, push (low_index, low_val) into the low-pivot list.
      Symmetric for down.
    """

    def __init__(self, zigzag_len: int = 40) -> None:
        self.zigzag_len = zigzag_len
        self.bars: List[Bar] = []

        # nz(trend[1], 1) — Pine seeds trend at 1.
        self.trend: int = 1

        # last bar where to_up / to_down was True.
        self.last_to_up_bar: Optional[int] = None
        self.last_to_down_bar: Optional[int] = None

        # zigzag pivot lists
        self.high_points: List[Tuple[int, float]] = []   # (bar_index, price)
        self.low_points: List[Tuple[int, float]] = []

        # running extremes within the active leg
        self.current_up_high: Optional[float] = None
        self.current_up_high_index: Optional[int] = None
        self.current_down_low: Optional[float] = None
        self.current_down_low_index: Optional[int] = None

        # fib state
        self.bull_fib_active: bool = False
        self.bull_fib_low: Optional[float] = None
        self.bull_fib_low_index: Optional[int] = None
        self.bull_fib_high: Optional[float] = None
        self.bull_fib_high_index: Optional[int] = None

        self.bear_fib_active: bool = False
        self.bear_fib_high: Optional[float] = None
        self.bear_fib_high_index: Optional[int] = None
        self.bear_fib_low: Optional[float] = None
        self.bear_fib_low_index: Optional[int] = None

        # HH* / LL* labels (Pine has dedicated single labels that move).
        # We map them onto the structure_events timeline by overwriting the
        # latest pending HH or LL entry whenever the running extreme advances.
        self._hh_star_event_idx: Optional[int] = None
        self._ll_star_event_idx: Optional[int] = None

        self.fib_state = FibState()
        self.last_structure_event: Optional[str] = None
        self.structure_events: List[Tuple[int, float, str]] = []
        self.fvg_events: List[Tuple[int, float, float, str]] = []

    def feed(self, bar: Bar) -> None:
        self.bars.append(bar)
        i = len(self.bars) - 1
        n = self.zigzag_len

        # Pine's ta.highest/ta.lowest return na until N bars are available.
        if i + 1 < n:
            return

        highest_n = max(b.high for b in self.bars[-n:])
        lowest_n = min(b.low for b in self.bars[-n:])
        to_up = bar.high >= highest_n
        to_down = bar.low <= lowest_n

        # Trend update — Pine evaluates the "down" branch first.
        prev_trend = self.trend
        if self.trend == 1 and to_down:
            self.trend = -1
        elif self.trend == -1 and to_up:
            self.trend = 1
        trend_changed = self.trend != prev_trend

        # --- Compute pivot for the just-completed leg ----------------
        # Pine: ta.barssince(to_up[1]) at bar i = i - (last_to_up_bar + 1).
        # That value is the lookback length for ta.lowest. We then scan that
        # window and pick the rightmost occurrence of the minimum (Pine uses
        # ta.barssince(low_val == low), which is the *most recent* match).
        def _scan(start_idx: int, end_idx: int, kind: str) -> Tuple[Optional[int], Optional[float]]:
            best_val: Optional[float] = None
            best_idx: Optional[int] = None
            for k in range(start_idx, end_idx + 1):
                v = self.bars[k].low if kind == "low" else self.bars[k].high
                if best_val is None:
                    best_val, best_idx = v, k
                else:
                    if kind == "low" and v <= best_val:
                        best_val, best_idx = v, k
                    elif kind == "high" and v >= best_val:
                        best_val, best_idx = v, k
            return best_idx, best_val

        # First trend-change block (Pine: push pivots).
        if trend_changed:
            if self.trend == 1:
                # Up leg started — pivot is the low of the just-finished down leg.
                if self.last_to_up_bar is None:
                    lookback = 1
                else:
                    lookback = max(1, i - self.last_to_up_bar - 1)
                start = max(0, i - lookback + 1)
                idx, val = _scan(start, i, "low")
                if idx is not None and val is not None:
                    self.low_points.append((idx, val))
            else:  # trend now -1
                if self.last_to_down_bar is None:
                    lookback = 1
                else:
                    lookback = max(1, i - self.last_to_down_bar - 1)
                start = max(0, i - lookback + 1)
                idx, val = _scan(start, i, "high")
                if idx is not None and val is not None:
                    self.high_points.append((idx, val))

        # h0/h1/l0/l1 — newest and previous pivots.
        h0 = self.high_points[-1][1] if self.high_points else None
        h0i = self.high_points[-1][0] if self.high_points else None
        h1 = self.high_points[-2][1] if len(self.high_points) > 1 else None
        l0 = self.low_points[-1][1] if self.low_points else None
        l0i = self.low_points[-1][0] if self.low_points else None
        l1 = self.low_points[-2][1] if len(self.low_points) > 1 else None

        # Update running extremes for the active leg.
        if self.trend == 1:
            if self.current_up_high is None or bar.high >= self.current_up_high:
                self.current_up_high = bar.high
                self.current_up_high_index = i
        else:
            if self.current_down_low is None or bar.low <= self.current_down_low:
                self.current_down_low = bar.low
                self.current_down_low_index = i

        # HH* — fires while the active up leg surpasses the previous high pivot.
        if (
            self.trend == 1
            and h0 is not None and l0 is not None
            and self.current_up_high is not None
            and self.current_up_high > h0
            and l0i is not None
            and self.current_up_high_index is not None
            and l0i < self.current_up_high_index
        ):
            self.bull_fib_active = True
            self.bull_fib_low = l0
            self.bull_fib_low_index = l0i
            self.bull_fib_high = self.current_up_high
            self.bull_fib_high_index = self.current_up_high_index
            self.last_structure_event = "HH*"
            ev = (self.current_up_high_index, self.current_up_high, "HH")
            if self._hh_star_event_idx is not None and 0 <= self._hh_star_event_idx < len(self.structure_events):
                self.structure_events[self._hh_star_event_idx] = ev
            else:
                self.structure_events.append(ev)
                self._hh_star_event_idx = len(self.structure_events) - 1

        # LL* — symmetric.
        if (
            self.trend == -1
            and l0 is not None and h0 is not None
            and self.current_down_low is not None
            and self.current_down_low < l0
            and h0i is not None
            and self.current_down_low_index is not None
            and h0i < self.current_down_low_index
        ):
            self.bear_fib_active = True
            self.bear_fib_high = h0
            self.bear_fib_high_index = h0i
            self.bear_fib_low = self.current_down_low
            self.bear_fib_low_index = self.current_down_low_index
            self.last_structure_event = "LL*"
            ev = (self.current_down_low_index, self.current_down_low, "LL")
            if self._ll_star_event_idx is not None and 0 <= self._ll_star_event_idx < len(self.structure_events):
                self.structure_events[self._ll_star_event_idx] = ev
            else:
                self.structure_events.append(ev)
                self._ll_star_event_idx = len(self.structure_events) - 1

        # Fib invalidations.
        if (
            self.bull_fib_active
            and self.bull_fib_low is not None
            and bar.low < self.bull_fib_low
        ):
            self.bull_fib_active = False
        if (
            self.bear_fib_active
            and self.bear_fib_high is not None
            and bar.high > self.bear_fib_high
        ):
            self.bear_fib_active = False

        # Second trend-change block — emit final HH / HL / LL / LH labels and
        # (re)activate the matching fib.
        if trend_changed:
            if self.trend == 1:
                # Reset up tracker to the current bar.
                self.current_up_high = bar.high
                self.current_up_high_index = i

                if l0 is not None and l1 is not None and l0i is not None:
                    if l0 < l1:
                        self.last_structure_event = "LL"
                        # Pine: delete LL* and emit LL (could be the same bar).
                        if (
                            self._ll_star_event_idx is not None
                            and 0 <= self._ll_star_event_idx < len(self.structure_events)
                            and self.structure_events[self._ll_star_event_idx][0] == l0i
                        ):
                            self.structure_events[self._ll_star_event_idx] = (l0i, l0, "LL")
                        else:
                            self.structure_events.append((l0i, l0, "LL"))
                        self._ll_star_event_idx = None
                        # Activate bearish fib at this trend change.
                        if h0 is not None and h0i is not None:
                            self.bear_fib_active = True
                            self.bear_fib_high = h0
                            self.bear_fib_high_index = h0i
                            self.bear_fib_low = l0
                            self.bear_fib_low_index = l0i
                    else:
                        self.last_structure_event = "HL"
                        if (
                            self._ll_star_event_idx is not None
                            and 0 <= self._ll_star_event_idx < len(self.structure_events)
                            and self.structure_events[self._ll_star_event_idx][0] == l0i
                        ):
                            self.structure_events[self._ll_star_event_idx] = (l0i, l0, "HL")
                        else:
                            self.structure_events.append((l0i, l0, "HL"))
                        self._ll_star_event_idx = None
            else:
                # Trend flipped to -1.
                self.current_down_low = bar.low
                self.current_down_low_index = i

                if h0 is not None and h1 is not None and h0i is not None:
                    if h0 > h1:
                        self.last_structure_event = "HH"
                        if (
                            self._hh_star_event_idx is not None
                            and 0 <= self._hh_star_event_idx < len(self.structure_events)
                            and self.structure_events[self._hh_star_event_idx][0] == h0i
                        ):
                            self.structure_events[self._hh_star_event_idx] = (h0i, h0, "HH")
                        else:
                            self.structure_events.append((h0i, h0, "HH"))
                        self._hh_star_event_idx = None
                        if l0 is not None and l0i is not None:
                            self.bull_fib_active = True
                            self.bull_fib_low = l0
                            self.bull_fib_low_index = l0i
                            self.bull_fib_high = h0
                            self.bull_fib_high_index = h0i
                    else:
                        self.last_structure_event = "LH"
                        if (
                            self._hh_star_event_idx is not None
                            and 0 <= self._hh_star_event_idx < len(self.structure_events)
                            and self.structure_events[self._hh_star_event_idx][0] == h0i
                        ):
                            self.structure_events[self._hh_star_event_idx] = (h0i, h0, "LH")
                        else:
                            self.structure_events.append((h0i, h0, "LH"))
                        self._hh_star_event_idx = None

        # Track running fib anchors so the snapshot reflects the last leg.
        if self.bull_fib_active and self.current_up_high is not None:
            if self.bull_fib_high is None or self.current_up_high > self.bull_fib_high:
                self.bull_fib_high = self.current_up_high
                self.bull_fib_high_index = self.current_up_high_index
        if self.bear_fib_active and self.current_down_low is not None:
            if self.bear_fib_low is None or self.current_down_low < self.bear_fib_low:
                self.bear_fib_low = self.current_down_low
                self.bear_fib_low_index = self.current_down_low_index

        # Compose snapshot fib_state — most recent active fib wins.
        if self.bull_fib_active and self.bull_fib_high is not None and self.bull_fib_low is not None:
            self.fib_state = FibState(
                direction="bullish",
                swing_low=self.bull_fib_low,
                fib_high=self.bull_fib_high,
                last_ms_bar=i,
            )
        elif self.bear_fib_active and self.bear_fib_high is not None and self.bear_fib_low is not None:
            self.fib_state = FibState(
                direction="bearish",
                swing_high=self.bear_fib_high,
                fib_low=self.bear_fib_low,
                last_ms_bar=i,
            )
        else:
            self.fib_state = FibState()

        # FVG (kazus_local also benefits from the same 3-bar gap rule).
        if i >= 2:
            two_back = self.bars[i - 2]
            if bar.low > two_back.high:
                self.fvg_events.append((i - 2, bar.low, two_back.high, "bullish"))
            if bar.high < two_back.low:
                self.fvg_events.append((i - 2, two_back.low, bar.high, "bearish"))

        # Update last_to_up_bar / last_to_down_bar AT THE END so that
        # ta.barssince(to_up[1]) on the next bar uses the correct value.
        if to_up:
            self.last_to_up_bar = i
        if to_down:
            self.last_to_down_bar = i

    def snapshot(self, price: float) -> ZoneResult:
        return _zone_result(self.fib_state, price)


# ---------------------------------------------------------------------------


def _zone_result(state: FibState, price: float) -> ZoneResult:
    if state.direction == "none":
        return ZoneResult(
            zone="none",
            in_ote=False,
            setup="no",
            retracement=None,
            direction="none",
            fib_low=None,
            fib_high=None,
            ote_low_price=None,
            ote_high_price=None,
        )

    ret = state.retracement_for(price)
    zone = classify_zone(ret)
    in_ote, setup = detect_setup(ret)

    anchors = state.active_anchors()
    if anchors is None:
        return ZoneResult(
            zone="none",
            in_ote=False,
            setup="no",
            retracement=None,
            direction=state.direction,
            fib_low=None,
            fib_high=None,
            ote_low_price=None,
            ote_high_price=None,
        )
    fib0, fib100 = anchors
    ote_low_price = fib0 + (fib100 - fib0) * OTE_LOW
    ote_high_price = fib0 + (fib100 - fib0) * OTE_HIGH
    lo, hi = sorted([ote_low_price, ote_high_price])

    return ZoneResult(
        zone=zone,
        in_ote=in_ote,
        setup=setup,
        retracement=ret,
        direction=state.direction,
        fib_low=min(fib0, fib100),
        fib_high=max(fib0, fib100),
        ote_low_price=lo,
        ote_high_price=hi,
    )
