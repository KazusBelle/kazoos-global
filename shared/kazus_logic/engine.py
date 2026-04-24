"""
Bar-by-bar implementation of Kazus Global (D1) and Kazus Local (H1) logic,
ported from the provided Pine Script sources.

Two engines are exposed:

- KazusGlobalEngine — mirrors `kazus_global_v3.pine`
  Uses HTF-style engulfing detection to produce bullish/bearish MS events,
  then locates local extremes between MS events and anchors a Fibonacci
  retracement from the swing base to the tracked post-MS extreme.

- KazusLocalEngine — mirrors `kazus_local_beta.pine`
  Classic zigzag with length N; active bullish fib while highs make HH,
  active bearish fib while lows make LL. Fib invalidates on swing break.

Each engine is fed bars one by one via `feed(bar)`. After each call the
current fibonacci anchors and zone classification are available through
`snapshot()`.

Pine source of truth notes:
- Pine's `ta.lowestSince(bearishMS, low)` = lowest low since last bearishMS,
  inclusive of the bar the event fired on.
- `ta.highestSince(bullishMS, high)` analogously.
- `lastSwingLow`/`lastSwingHigh` are updated at the MS event, using the
  local extreme discovered between the previous opposite-BoS bar and the
  bar on which MS fired.

Where Pine behavior was ambiguous we fell back to the simpler consistent
interpretation and documented it in README assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Zone classification (shared between both engines)
# ---------------------------------------------------------------------------

# User-stated assumption: treat 0.5–0.61 as discount.
# Equilibrium is a narrow band around 0.5.
EQUILIBRIUM_LOW = 0.48
EQUILIBRIUM_HIGH = 0.52
OTE_LOW = 0.62
OTE_HIGH = 0.79


@dataclass
class ZoneResult:
    zone: str                 # "premium" | "equilibrium" | "discount" | "none"
    in_ote: bool
    setup: str                # "yes" | "no"
    retracement: Optional[float]  # 0.0 .. 1.0 or None
    direction: str            # "bullish" | "bearish" | "none"
    fib_low: Optional[float]
    fib_high: Optional[float]
    ote_low_price: Optional[float]
    ote_high_price: Optional[float]


def classify_zone(retracement: Optional[float]) -> str:
    if retracement is None:
        return "none"
    if EQUILIBRIUM_LOW <= retracement <= EQUILIBRIUM_HIGH:
        return "equilibrium"
    if retracement < EQUILIBRIUM_LOW:
        return "premium"
    return "discount"


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
    direction: str = "none"       # "bullish" | "bearish" | "none"
    swing_low: Optional[float] = None     # anchor on bullish (1.0 in Pine)
    swing_high: Optional[float] = None    # anchor on bearish (1.0 in Pine)
    fib_low: Optional[float] = None       # anchor-0 on bearish
    fib_high: Optional[float] = None      # anchor-0 on bullish
    last_ms_bar: Optional[int] = None

    def active_anchors(self) -> Optional[Tuple[float, float]]:
        """Return (fib0_price, fib100_price) or None if not active."""
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
    Port of kazus_global_v3.pine. Intended to be fed D1 bars; the engine
    does not itself request HTF data since the caller supplies already-HTF
    bars (confirmed D1 candles).

    Call .feed(bar) for each bar in chronological order.
    After each call the current state is available via .fib_state, and
    .snapshot(current_price) returns the zone classification.
    """

    def __init__(self) -> None:
        self.bars: List[Bar] = []

        # engulfing / signal state
        self.last_signal: int = 0            # 0, 1 (bull), -1 (bear)
        self.running_lowest_high: Optional[float] = None
        self.running_highest_low: Optional[float] = None
        self.last_bull_index: Optional[int] = None
        self.last_bear_index: Optional[int] = None

        # MS state
        self.last_ms_type: Optional[int] = None  # 1 bull MS, -1 bear MS
        self.last_bullish_bos_bar: Optional[int] = None
        self.last_bearish_bos_bar: Optional[int] = None

        # swings
        self.prev_swing_high: Optional[float] = None
        self.prev_swing_low: Optional[float] = None
        self.last_swing_high: Optional[float] = None
        self.last_swing_low: Optional[float] = None

        # local high/low tracked after internal shifts (used to seed MS detection)
        self.local_high: Optional[float] = None
        self.local_high_index: Optional[int] = None
        self.local_low: Optional[float] = None
        self.local_low_index: Optional[int] = None

        # tracked post-MS fibonacci anchors
        self.tracked_fib_high: Optional[float] = None
        self.tracked_fib_high_index: Optional[int] = None
        self.tracked_fib_low: Optional[float] = None
        self.tracked_fib_low_index: Optional[int] = None

        self.fib_state = FibState()

        # structure history (HH/LL/HL/LH) — the worker only needs the last events
        self.last_structure_event: Optional[str] = None

    # -- helpers ------------------------------------------------------------

    def _prev(self, offset: int = 1) -> Optional[Bar]:
        idx = len(self.bars) - 1 - offset
        return self.bars[idx] if idx >= 0 else None

    # -- main feed ----------------------------------------------------------

    def feed(self, bar: Bar) -> None:
        self.bars.append(bar)
        i = len(self.bars) - 1
        prev1 = self._prev(1)

        # --- engulfing detection -----------------------------------------
        starter_bull = False
        starter_bear = False
        if prev1 is not None:
            starter_bull = (
                prev1.close < prev1.open
                and bar.close > bar.open
                and bar.close > prev1.high
            )
            starter_bear = (
                prev1.close > prev1.open
                and bar.close < bar.open
                and bar.close < prev1.low
            )

        if self.last_signal == 0:
            if starter_bull and prev1 is not None:
                self.last_signal = 1
                self.running_highest_low = bar.low
            elif starter_bear and prev1 is not None:
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
            self.last_signal = 1
            self.running_lowest_high = None
            self.running_highest_low = bar.low

        if new_bear:
            self.last_bear_index = i
            self.last_signal = -1
            self.running_highest_low = None
            self.running_lowest_high = bar.high

        # --- local extremes between internal shifts ----------------------
        if new_bear:
            # find highest high since last_bull_index (exclusive range)
            start = self.last_bull_index if self.last_bull_index is not None else 0
            hh, hh_idx = None, None
            for k in range(start, i + 1):
                h = self.bars[k].high
                if hh is None or h > hh:
                    hh, hh_idx = h, k
            self.local_high = hh
            self.local_high_index = hh_idx

        if new_bull:
            start = self.last_bear_index if self.last_bear_index is not None else 0
            ll, ll_idx = None, None
            for k in range(start, i + 1):
                lo = self.bars[k].low
                if ll is None or lo < ll:
                    ll, ll_idx = lo, k
            self.local_low = ll
            self.local_low_index = ll_idx

        # --- Market Structure: close > local_high / close < local_low ---
        can_bull_ms = self.last_ms_type is None or self.last_ms_type == -1
        can_bear_ms = self.last_ms_type is None or self.last_ms_type == 1

        bullish_ms = (
            self.local_high is not None
            and bar.close > self.local_high
            and can_bull_ms
        )
        bearish_ms = (
            self.local_low is not None
            and bar.close < self.local_low
            and can_bear_ms
        )

        if bullish_ms:
            self.last_ms_type = 1
            self.last_bullish_bos_bar = i

            # find lowest low since last bearish BoS bar (inclusive)
            start = self.last_bearish_bos_bar if self.last_bearish_bos_bar is not None else 0
            ms_low, ms_low_idx = None, None
            for k in range(start, i + 1):
                lo = self.bars[k].low
                if ms_low is None or lo < ms_low:
                    ms_low, ms_low_idx = lo, k

            # structure classification
            is_ll = self.prev_swing_low is not None and ms_low < self.prev_swing_low
            is_hl = self.prev_swing_low is not None and ms_low > self.prev_swing_low
            if is_ll:
                self.last_structure_event = "LL"
            elif is_hl:
                self.last_structure_event = "HL"

            self.prev_swing_low = ms_low
            self.last_swing_low = ms_low

            # reset tracked fib high
            self.tracked_fib_high = bar.high
            self.tracked_fib_high_index = i
            self.fib_state = FibState(
                direction="bullish",
                swing_low=ms_low,
                fib_high=bar.high,
                last_ms_bar=i,
            )

        if bearish_ms:
            self.last_ms_type = -1
            self.last_bearish_bos_bar = i

            start = self.last_bullish_bos_bar if self.last_bullish_bos_bar is not None else 0
            ms_high, ms_high_idx = None, None
            for k in range(start, i + 1):
                h = self.bars[k].high
                if ms_high is None or h > ms_high:
                    ms_high, ms_high_idx = h, k

            is_hh = self.prev_swing_high is not None and ms_high > self.prev_swing_high
            is_lh = self.prev_swing_high is not None and ms_high < self.prev_swing_high
            if is_hh:
                self.last_structure_event = "HH"
            elif is_lh:
                self.last_structure_event = "LH"

            self.prev_swing_high = ms_high
            self.last_swing_high = ms_high

            self.tracked_fib_low = bar.low
            self.tracked_fib_low_index = i
            self.fib_state = FibState(
                direction="bearish",
                swing_high=ms_high,
                fib_low=bar.low,
                last_ms_bar=i,
            )

        # --- update tracked anchors (monotonic within active direction) --
        if self.fib_state.direction == "bullish":
            if self.tracked_fib_high is None or bar.high >= self.tracked_fib_high:
                self.tracked_fib_high = bar.high
                self.tracked_fib_high_index = i
            self.fib_state.fib_high = self.tracked_fib_high
            self.fib_state.swing_low = self.last_swing_low
        elif self.fib_state.direction == "bearish":
            if self.tracked_fib_low is None or bar.low <= self.tracked_fib_low:
                self.tracked_fib_low = bar.low
                self.tracked_fib_low_index = i
            self.fib_state.fib_low = self.tracked_fib_low
            self.fib_state.swing_high = self.last_swing_high

    # -- snapshot -----------------------------------------------------------

    def snapshot(self, price: float) -> ZoneResult:
        return _zone_result(self.fib_state, price)


# ---------------------------------------------------------------------------
# Kazus Local (H1) — zigzag-based
# ---------------------------------------------------------------------------

class KazusLocalEngine:
    """
    Port of kazus_local_beta.pine. Fed with H1 bars.

    ZigZag length = zigzag_len (default 40).

    The fib is activated when a new HH or LL* is formed and remains active
    until its corresponding anchor low/high is violated by the price.
    """

    def __init__(self, zigzag_len: int = 40) -> None:
        self.zigzag_len = zigzag_len
        self.bars: List[Bar] = []

        self.trend: int = 1  # 1 up, -1 down (Pine nz defaults to 1)

        self.high_points: List[Tuple[int, float]] = []  # (bar_index, high)
        self.low_points: List[Tuple[int, float]] = []

        self.current_up_high: Optional[float] = None
        self.current_up_high_index: Optional[int] = None
        self.current_down_low: Optional[float] = None
        self.current_down_low_index: Optional[int] = None

        self.fib_state = FibState()

        # Tracks for active bull/bear fib that invalidates on break.
        # Bullish fib uses (bullFibLow, bullFibHigh); if low < bullFibLow → inactive.
        self.bull_fib_low: Optional[float] = None
        self.bull_fib_high: Optional[float] = None
        self.bear_fib_high: Optional[float] = None
        self.bear_fib_low: Optional[float] = None

        self.last_structure_event: Optional[str] = None

    def _highest(self, n: int) -> Optional[float]:
        if len(self.bars) < n:
            return None
        return max(b.high for b in self.bars[-n:])

    def _lowest(self, n: int) -> Optional[float]:
        if len(self.bars) < n:
            return None
        return min(b.low for b in self.bars[-n:])

    def feed(self, bar: Bar) -> None:
        self.bars.append(bar)
        i = len(self.bars) - 1
        n = self.zigzag_len

        if len(self.bars) < n:
            return

        highest_n = self._highest(n)
        lowest_n = self._lowest(n)
        to_up = bar.high >= highest_n
        to_down = bar.low <= lowest_n

        prev_trend = self.trend
        if self.trend == 1 and to_down:
            new_trend = -1
        elif self.trend == -1 and to_up:
            new_trend = 1
        else:
            new_trend = self.trend

        trend_changed = new_trend != prev_trend

        # When trend flips, identify the swing on the *previous* trend's side.
        if trend_changed:
            if new_trend == 1:
                # uptrend starts → record the low of the down leg
                lookback_start = max(0, len(self.bars) - n - 1)
                ll_val, ll_idx = None, None
                for k in range(lookback_start, i + 1):
                    lo = self.bars[k].low
                    if ll_val is None or lo < ll_val:
                        ll_val, ll_idx = lo, k
                if ll_val is not None and ll_idx is not None:
                    self.low_points.append((ll_idx, ll_val))
            else:
                lookback_start = max(0, len(self.bars) - n - 1)
                hh_val, hh_idx = None, None
                for k in range(lookback_start, i + 1):
                    h = self.bars[k].high
                    if hh_val is None or h > hh_val:
                        hh_val, hh_idx = h, k
                if hh_val is not None and hh_idx is not None:
                    self.high_points.append((hh_idx, hh_val))

        self.trend = new_trend

        # Update running extremes for current trend leg
        if self.trend == 1:
            if self.current_up_high is None or bar.high >= self.current_up_high:
                self.current_up_high = bar.high
                self.current_up_high_index = i
        else:
            if self.current_down_low is None or bar.low <= self.current_down_low:
                self.current_down_low = bar.low
                self.current_down_low_index = i

        h0 = self.high_points[-1][1] if self.high_points else None
        h0i = self.high_points[-1][0] if self.high_points else None
        l0 = self.low_points[-1][1] if self.low_points else None
        l0i = self.low_points[-1][0] if self.low_points else None
        h1 = self.high_points[-2][1] if len(self.high_points) > 1 else None
        l1 = self.low_points[-2][1] if len(self.low_points) > 1 else None

        # HH*/LL* star conditions (current trend extreme taking previous swing)
        if (
            self.trend == 1
            and h0 is not None
            and l0 is not None
            and self.current_up_high is not None
            and self.current_up_high > h0
            and l0i is not None
            and self.current_up_high_index is not None
            and l0i < self.current_up_high_index
        ):
            self.bull_fib_low = l0
            self.bull_fib_high = self.current_up_high
            self.fib_state = FibState(
                direction="bullish",
                swing_low=l0,
                fib_high=self.current_up_high,
                last_ms_bar=i,
            )
            self.last_structure_event = "HH*"

        if (
            self.trend == -1
            and h0 is not None
            and l0 is not None
            and self.current_down_low is not None
            and self.current_down_low < l0
            and h0i is not None
            and self.current_down_low_index is not None
            and h0i < self.current_down_low_index
        ):
            self.bear_fib_high = h0
            self.bear_fib_low = self.current_down_low
            self.fib_state = FibState(
                direction="bearish",
                swing_high=h0,
                fib_low=self.current_down_low,
                last_ms_bar=i,
            )
            self.last_structure_event = "LL*"

        # Invalidations
        if (
            self.fib_state.direction == "bullish"
            and self.bull_fib_low is not None
            and bar.low < self.bull_fib_low
        ):
            self.fib_state = FibState()
            self.bull_fib_low = None
            self.bull_fib_high = None

        if (
            self.fib_state.direction == "bearish"
            and self.bear_fib_high is not None
            and bar.high > self.bear_fib_high
        ):
            self.fib_state = FibState()
            self.bear_fib_high = None
            self.bear_fib_low = None

        # On trend change with valid pivots — re-evaluate HH/HL or LL/LH
        if trend_changed:
            if self.trend == 1 and l0 is not None and l1 is not None:
                if l0 < l1:
                    self.last_structure_event = "LL"
                    # activate bearish fib
                    if h0 is not None:
                        self.bear_fib_high = h0
                        self.bear_fib_low = l0
                        self.fib_state = FibState(
                            direction="bearish",
                            swing_high=h0,
                            fib_low=l0,
                            last_ms_bar=i,
                        )
                else:
                    self.last_structure_event = "HL"
                # reset up-tracker
                self.current_up_high = bar.high
                self.current_up_high_index = i
            elif self.trend == -1 and h0 is not None and h1 is not None:
                if h0 > h1:
                    self.last_structure_event = "HH"
                    if l0 is not None:
                        self.bull_fib_low = l0
                        self.bull_fib_high = h0
                        self.fib_state = FibState(
                            direction="bullish",
                            swing_low=l0,
                            fib_high=h0,
                            last_ms_bar=i,
                        )
                else:
                    self.last_structure_event = "LH"
                self.current_down_low = bar.low
                self.current_down_low_index = i

        # Keep fib anchors tracked against the active extreme on the current leg
        if self.fib_state.direction == "bullish" and self.current_up_high is not None:
            if (
                self.bull_fib_high is None
                or self.current_up_high > self.bull_fib_high
            ):
                self.bull_fib_high = self.current_up_high
            self.fib_state.fib_high = self.bull_fib_high
            self.fib_state.swing_low = self.bull_fib_low
        elif self.fib_state.direction == "bearish" and self.current_down_low is not None:
            if (
                self.bear_fib_low is None
                or self.current_down_low < self.bear_fib_low
            ):
                self.bear_fib_low = self.current_down_low
            self.fib_state.fib_low = self.bear_fib_low
            self.fib_state.swing_high = self.bear_fib_high

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
