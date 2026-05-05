"""
High-level 'compute snapshot for a symbol' helper that wires a Binance
kline fetch through the appropriate engine and returns both the Global
(D1) and Local (H1) results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .binance import BinanceFuturesClient
from .engine import (
    Bar,
    KazusGlobalEngine,
    KazusLocalEngine,
    ZoneResult,
)

# Length of the sparkline series we expose through the API.
SPARKLINE_LEN = 40

# Number of recent confirmation bars to scan for FVG detection.
FVG_LOOKBACK = 100

SETUP_DIRECTIONS: Tuple[str, ...] = ("bullish", "bearish")

SETUP_INVERSION = "Inversion"
SETUP_CREATED = "Created"
SETUP_STB = "STB"

# Priority order used when picking a single setup label for the snapshot UI.
_SETUP_PRIORITY = {SETUP_STB: 3, SETUP_INVERSION: 2, SETUP_CREATED: 1}


@dataclass(frozen=True)
class SetupEvent:
    """
    A single alertable setup occurrence on the last closed confirmation bar.

    `event_id` is a stable string keyed off bar open-times (Binance ms epoch),
    so the same event observed across multiple worker cycles produces the
    same id and won't re-fire. The worker stores fired ids per
    (symbol, timeframe) and clears them when price leaves OTE.
    """
    kind: str                       # "STB" | "Inversion" | "Created"
    event_id: str
    fvg_top: float
    fvg_bottom: float
    fvg_kind: str                   # "bullish" | "bearish"
    trigger_open_ms: int
    direction: str                  # "bullish" | "bearish"
    # Additional event_ids to mark as sent alongside this one. STB uses this
    # to suppress the redundant Created/Inversion ids on the same bar / FVG.
    suppresses: Tuple[str, ...] = ()


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float
    global_result: ZoneResult
    local_result: ZoneResult
    global_trend: str        # "up" | "down" | "none"
    local_trend: str
    global_closes: List[float] = field(default_factory=list)
    local_closes: List[float] = field(default_factory=list)
    # Confirmation bars used for setup detection — H1 for Global, M15 for
    # Local. Worker keeps these around to render the alert chart image.
    global_confirmation_bars: List[Bar] = field(default_factory=list)
    local_confirmation_bars: List[Bar] = field(default_factory=list)
    # HTF bars (D1 for Global, H1 for Local) for the alert HTF chart image.
    global_htf_bars: List[Bar] = field(default_factory=list)
    local_htf_bars: List[Bar] = field(default_factory=list)
    # Setup events emitted by the last-closed trigger bar. Worker dedupes
    # against AlertState.sent_event_ids and fires one Telegram message per
    # new event.
    global_setup_events: List["SetupEvent"] = field(default_factory=list)
    local_setup_events: List["SetupEvent"] = field(default_factory=list)


def _trend_from_event(ev: Optional[str]) -> str:
    if ev in ("HH", "HL", "HH*"):
        return "up"
    if ev in ("LL", "LH", "LL*"):
        return "down"
    return "none"


def _scan_fvgs(bars: List[Bar]) -> List[Tuple[int, float, float, str]]:
    """
    Return all 3-bar FVGs found in `bars`.
    Each entry: (i, top, bottom, kind). `i` indexes `bars`. The gap is
    formed on bar i (rule of three), measured against bar i-2.
      - bullish: bars[i].low > bars[i-2].high  -> [bars[i-2].high, bars[i].low]
      - bearish: bars[i].high < bars[i-2].low  -> [bars[i].high,   bars[i-2].low]
    """
    out: List[Tuple[int, float, float, str]] = []
    for i in range(2, len(bars)):
        if bars[i].low > bars[i - 2].high:
            out.append((i, bars[i].low, bars[i - 2].high, "bullish"))
        if bars[i].high < bars[i - 2].low:
            out.append((i, bars[i - 2].low, bars[i].high, "bearish"))
    return out


def _is_fvg_mitigated(bars: List[Bar], fvg_idx: int, fvg_top: float, fvg_bottom: float) -> bool:
    """
    A simple mitigation check: any later bar whose range fully spans the gap
    interior invalidates it. Returns True when any bar after the formation
    has its low <= fvg_top AND high >= fvg_bottom (touched the gap interior).
    """
    for j in range(fvg_idx + 1, len(bars)):
        if bars[j].low <= fvg_top and bars[j].high >= fvg_bottom:
            return True
    return False


def detect_setup_events(
    zone_result: ZoneResult,
    confirmation_bars: List[Bar],
    symbol: str = "",
    timeframe: str = "",
) -> List[SetupEvent]:
    """
    Emit zero or more SetupEvent objects from the last closed confirmation
    bar. Direction-aware (mirror logic for bullish vs bearish setups).

    Per-bar rules:
      - `fresh`   : an FVG formed exactly on the trigger bar.
      - `reclaim` : the closest unmitigated FVG strictly before the trigger
                    that the trigger body reclaims.
      - **STB**       — fresh exists AND the body reclaims fresh on the
                        same bar (impulse).  Suppresses both Created and
                        Inversion ids for that bar/FVG.
      - **Created**   — fresh exists but body does not reclaim it.
      - **Inversion** — reclaim exists.

    Geometry by direction:
      - bearish setup (short, retracement up):
          body_low > fvg.top   -> reclaimed
          fresh kind preference: bullish FVG (gap-up retrace)
      - bullish setup (long, retracement down):
          body_high < fvg.bottom -> reclaimed
          fresh kind preference: bearish FVG (gap-down retrace)

    Inversion fires at most once per cycle (closest reclaimed FVG below /
    above the body) — by user spec, one alert per zone, not per bar.
    """
    if (
        not zone_result.in_ote
        or not confirmation_bars
        or zone_result.direction not in SETUP_DIRECTIONS
    ):
        return []

    scan = (
        confirmation_bars[-FVG_LOOKBACK:]
        if len(confirmation_bars) > FVG_LOOKBACK
        else list(confirmation_bars)
    )
    if len(scan) < 3:
        return []

    trigger_idx = len(scan) - 1
    trigger = scan[trigger_idx]
    body_low = min(trigger.open, trigger.close)
    body_high = max(trigger.open, trigger.close)
    direction = zone_result.direction
    trigger_ts = int(trigger.ts)

    fvgs = _scan_fvgs(scan)

    def _body_reclaims(top: float, bottom: float) -> bool:
        if direction == "bearish":
            return body_low > top
        return body_high < bottom

    # --- pick the fresh FVG on the trigger bar (at most one per direction) -
    fresh_all = [c for c in fvgs if c[0] == trigger_idx]
    fresh: Optional[Tuple[int, float, float, str]] = None
    if fresh_all:
        # Prefer bullish FVG for a bearish setup (gap-up retrace) and
        # bearish FVG for a bullish setup (gap-down retrace).
        preferred_kind = "bullish" if direction == "bearish" else "bearish"
        fresh_sorted = sorted(
            fresh_all, key=lambda c: 0 if c[3] == preferred_kind else 1
        )
        fresh = fresh_sorted[0]

    # --- pick the closest reclaimed FVG strictly before the trigger --------
    reclaim_candidates = [
        (i, top, bottom, kind)
        for (i, top, bottom, kind) in fvgs
        if i < trigger_idx
        and _body_reclaims(top, bottom)
        and not _is_fvg_mitigated(scan, i, top, bottom)
    ]
    reclaim: Optional[Tuple[int, float, float, str]] = None
    if reclaim_candidates:
        if direction == "bearish":
            # Closest below body = highest top.
            reclaim = max(reclaim_candidates, key=lambda c: c[1])
        else:
            # Closest above body = lowest bottom.
            reclaim = min(reclaim_candidates, key=lambda c: c[2])

    events: List[SetupEvent] = []
    prefix = f"{symbol}:{timeframe}" if (symbol or timeframe) else ""

    def _id(kind_prefix: str, anchor_ts: int) -> str:
        return f"{kind_prefix}:{prefix}:{anchor_ts}" if prefix else f"{kind_prefix}:{anchor_ts}"

    fresh_reclaimed = (
        fresh is not None and _body_reclaims(fresh[1], fresh[2])
    )

    if fresh is not None and fresh_reclaimed:
        # STB: fresh FVG + body reclaim on same bar.
        _i, top, bottom, fkind = fresh
        events.append(
            SetupEvent(
                kind=SETUP_STB,
                event_id=_id("stb", trigger_ts),
                fvg_top=top,
                fvg_bottom=bottom,
                fvg_kind=fkind,
                trigger_open_ms=trigger_ts,
                direction=direction,
                # Mark the redundant Created+Inversion ids for this bar/FVG
                # as sent so they never fire on later cycles.
                suppresses=(
                    _id("new", trigger_ts),
                    _id("inv", int(scan[fresh[0]].ts)),
                ),
            )
        )
    elif fresh is not None:
        # Created: fresh FVG without body reclaim.
        _i, top, bottom, fkind = fresh
        events.append(
            SetupEvent(
                kind=SETUP_CREATED,
                event_id=_id("new", trigger_ts),
                fvg_top=top,
                fvg_bottom=bottom,
                fvg_kind=fkind,
                trigger_open_ms=trigger_ts,
                direction=direction,
            )
        )

    if reclaim is not None:
        i, top, bottom, fkind = reclaim
        fvg_anchor_ts = int(scan[i].ts)
        events.append(
            SetupEvent(
                kind=SETUP_INVERSION,
                event_id=_id("inv", fvg_anchor_ts),
                fvg_top=top,
                fvg_bottom=bottom,
                fvg_kind=fkind,
                trigger_open_ms=trigger_ts,
                direction=direction,
            )
        )

    return events


def _promote_zone_result(
    zone_result: ZoneResult, events: List[SetupEvent]
) -> ZoneResult:
    """
    Pick the highest-priority event for the snapshot UI label so the screener
    keeps showing a single setup string (STB > Inversion > Created). The
    `setup_fvg_*` fields mirror the chosen event for any consumer that still
    reads them; the worker uses the events list directly for alerts.
    """
    if not events:
        return zone_result
    best = max(events, key=lambda e: _SETUP_PRIORITY.get(e.kind, 0))
    return ZoneResult(
        zone=zone_result.zone,
        in_ote=zone_result.in_ote,
        setup=best.kind,
        retracement=zone_result.retracement,
        direction=zone_result.direction,
        fib_low=zone_result.fib_low,
        fib_high=zone_result.fib_high,
        ote_low_price=zone_result.ote_low_price,
        ote_high_price=zone_result.ote_high_price,
        setup_fvg_top=best.fvg_top,
        setup_fvg_bottom=best.fvg_bottom,
    )


async def compute_symbol(
    client: BinanceFuturesClient,
    symbol: str,
    d1_limit: int = 500,
    h1_limit: int = 900,
    m15_limit: int = 600,
) -> SymbolSnapshot:
    """Compute the (D1, H1) snapshot for a symbol. Bootstraps both engines
    from raw klines and runs the FVG-based setup-event detector on the
    confirmation timeframes (H1 for Global, M15 for Local)."""
    d1_bars, h1_bars, m15_bars = await _fetch_all(
        client, symbol, d1_limit, h1_limit, m15_limit
    )

    # Drop the in-progress (last) bar — Binance returns it open.
    d1_closed = d1_bars[:-1] if len(d1_bars) > 1 else d1_bars
    h1_closed = h1_bars[:-1] if len(h1_bars) > 1 else h1_bars
    m15_closed = m15_bars[:-1] if len(m15_bars) > 1 else m15_bars

    g = KazusGlobalEngine()
    for b in d1_closed:
        g.feed(b)

    l = KazusLocalEngine()
    for b in h1_closed:
        l.feed(b)

    price = h1_bars[-1].close if h1_bars else 0.0

    global_result = g.snapshot(price)
    local_result = l.snapshot(price)

    # FVG-based setup detection:
    # Global (D1 OTE) confirmed by H1 bars; Local (H1 OTE) confirmed by M15.
    global_events = detect_setup_events(global_result, h1_closed, symbol, "D1")
    local_events = detect_setup_events(local_result, m15_closed, symbol, "H1")

    # Snapshot UI label: keep the highest-priority event visible in the
    # screener (STB > Inversion > Created). The full event list drives
    # the worker's per-event Telegram alerts.
    global_result = _promote_zone_result(global_result, global_events)
    local_result = _promote_zone_result(local_result, local_events)

    return SymbolSnapshot(
        symbol=symbol,
        price=price,
        global_result=global_result,
        local_result=local_result,
        global_trend=_trend_from_event(g.last_structure_event),
        local_trend=_trend_from_event(l.last_structure_event),
        global_closes=[b.close for b in d1_closed[-SPARKLINE_LEN:]],
        local_closes=[b.close for b in h1_closed[-SPARKLINE_LEN:]],
        global_confirmation_bars=h1_closed[-FVG_LOOKBACK:],
        local_confirmation_bars=m15_closed[-FVG_LOOKBACK:],
        global_htf_bars=list(d1_closed),
        local_htf_bars=list(h1_closed),
        global_setup_events=global_events,
        local_setup_events=local_events,
    )


async def _fetch_all(
    client: BinanceFuturesClient,
    symbol: str,
    d1_limit: int,
    h1_limit: int,
    m15_limit: int,
):
    import asyncio
    return await asyncio.gather(
        client.klines(symbol, "1d", limit=d1_limit),
        client.klines(symbol, "1h", limit=h1_limit),
        client.klines(symbol, "15m", limit=m15_limit),
    )
