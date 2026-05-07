"""
High-level 'compute snapshot for a symbol' helper.

Fetches Binance klines for the timeframes the (HTF, LTF) pairs need,
runs the appropriate engine to derive the OTE zone on each HTF, and
runs the long-only INV/CRE/STB setup detector on each LTF stream.

Pairs:
  D1 -> H1     (KazusGlobalEngine, alert tf "D1")
  H1 -> M15    (KazusLocalEngine,  alert tf "H1")
  H1 -> M5     (KazusLocalEngine,  alert tf "H1-M5")  — only for M5_SYMBOLS

The detector is pure but stateful across worker cycles: callers pass
`prev_states[tf]` (loaded from DB) and persist the returned `setup_states[tf]`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from .binance import BinanceFuturesClient
from .engine import (
    Bar,
    KazusGlobalEngine,
    KazusLocalEngine,
    ZoneResult,
)
from .setup import SetupEvent, SetupState, detect_setup

SPARKLINE_LEN = 40
FVG_LOOKBACK = 100

# Symbols that get the parallel H1->M5 setup-detection path on top of
# the standard H1->M15 path.
M5_SYMBOLS: FrozenSet[str] = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})

ALERT_TF_D1 = "D1"          # D1 OTE, H1 confirmation
ALERT_TF_H1 = "H1"          # H1 OTE, M15 confirmation
ALERT_TF_H1_M5 = "H1-M5"    # H1 OTE, M5 confirmation (BTC/ETH/SOL)

ALL_ALERT_TFS: Tuple[str, ...] = (ALERT_TF_D1, ALERT_TF_H1, ALERT_TF_H1_M5)

# Snapshot-row label priority — for screener column SETUP. STB > INV/CRE > NO.
_SETUP_PRIORITY: Dict[str, int] = {"STB": 3, "INV": 2, "CRE": 2, "NO": 0, "": 0}


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float

    # HTF zone results — direction, OTE bounds, etc.
    global_result: ZoneResult        # D1 zone
    local_result: ZoneResult         # H1 zone
    global_trend: str
    local_trend: str

    # Sparkline closes for the screener.
    global_closes: List[float] = field(default_factory=list)
    local_closes: List[float] = field(default_factory=list)

    # LTF confirmation bars per alert tf, used both by the detector
    # (already consumed) and by the alert chart renderer.
    confirmation_bars: Dict[str, List[Bar]] = field(default_factory=dict)
    # HTF context bars per alert tf, used by the alert chart renderer.
    htf_bars: Dict[str, List[Bar]] = field(default_factory=dict)

    # New post-cycle state machine and events, keyed by alert tf.
    # `setup_states` always contains an entry for every tf the cycle ran
    # through, even if no events fired (state may still have transitioned).
    setup_states: Dict[str, SetupState] = field(default_factory=dict)
    setup_events: Dict[str, List[SetupEvent]] = field(default_factory=dict)


def _trend_from_event(ev: Optional[str]) -> str:
    if ev in ("HH", "HL", "HH*"):
        return "up"
    if ev in ("LL", "LH", "LL*"):
        return "down"
    return "none"


def _label_for_tf(snap: SymbolSnapshot, tf: str) -> str:
    state = snap.setup_states.get(tf)
    return state.state if state else "NO"


def screener_label_for(snap: SymbolSnapshot, snapshot_row_tf: str) -> str:
    """Return the SETUP-column label for a screener row.

    snapshot_row_tf is the row's tf as stored in the `snapshots` table:
      "D1" -> reflects D1->H1 setup state.
      "H1" -> reflects H1->M15. For BTC/ETH/SOL also collapses H1-M5 in,
              picking whichever has the higher priority.
    """
    if snapshot_row_tf == "D1":
        return _label_for_tf(snap, ALERT_TF_D1)
    if snapshot_row_tf == "H1":
        a = _label_for_tf(snap, ALERT_TF_H1)
        b = _label_for_tf(snap, ALERT_TF_H1_M5)
        return max((a, b), key=lambda x: _SETUP_PRIORITY.get(x, 0))
    return "NO"


async def compute_symbol(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    d1_limit: int = 500,
    h1_limit: int = 900,
    m15_limit: int = 600,
    m5_limit: int = 600,
    prev_states: Optional[Dict[str, SetupState]] = None,
) -> SymbolSnapshot:
    """Fetch klines and compute the per-tf setup state.

    `prev_states` carries the SetupState for each alert tf from the previous
    worker cycle. Pass None on first run; subsequent runs should pass the
    states stored on AlertState.setup_state_json.
    """
    prev_states = prev_states or {}
    needs_m5 = symbol in M5_SYMBOLS

    fetches = [
        client.klines(symbol, "1d", limit=d1_limit),
        client.klines(symbol, "1h", limit=h1_limit),
        client.klines(symbol, "15m", limit=m15_limit),
    ]
    if needs_m5:
        fetches.append(client.klines(symbol, "5m", limit=m5_limit))

    import asyncio
    results = await asyncio.gather(*fetches)
    d1_bars, h1_bars, m15_bars = results[0], results[1], results[2]
    m5_bars: List[Bar] = results[3] if needs_m5 else []

    d1_closed = d1_bars[:-1] if len(d1_bars) > 1 else d1_bars
    h1_closed = h1_bars[:-1] if len(h1_bars) > 1 else h1_bars
    m15_closed = m15_bars[:-1] if len(m15_bars) > 1 else m15_bars
    m5_closed = m5_bars[:-1] if len(m5_bars) > 1 else m5_bars

    g = KazusGlobalEngine()
    for b in d1_closed:
        g.feed(b)
    l = KazusLocalEngine()
    for b in h1_closed:
        l.feed(b)

    price = h1_bars[-1].close if h1_bars else 0.0
    global_zone = g.snapshot(price)
    local_zone = l.snapshot(price)

    setup_states: Dict[str, SetupState] = {}
    setup_events: Dict[str, List[SetupEvent]] = {}

    # D1 OTE -> H1 setups.
    s_d1, e_d1 = detect_setup(
        global_zone, h1_closed, prev_states.get(ALERT_TF_D1),
        symbol=symbol, timeframe=ALERT_TF_D1,
    )
    setup_states[ALERT_TF_D1] = s_d1
    setup_events[ALERT_TF_D1] = e_d1

    # H1 OTE -> M15 setups.
    s_h1, e_h1 = detect_setup(
        local_zone, m15_closed, prev_states.get(ALERT_TF_H1),
        symbol=symbol, timeframe=ALERT_TF_H1,
    )
    setup_states[ALERT_TF_H1] = s_h1
    setup_events[ALERT_TF_H1] = e_h1

    # H1 OTE -> M5 setups (parallel, BTC/ETH/SOL only).
    if needs_m5:
        s_m5, e_m5 = detect_setup(
            local_zone, m5_closed, prev_states.get(ALERT_TF_H1_M5),
            symbol=symbol, timeframe=ALERT_TF_H1_M5,
        )
        setup_states[ALERT_TF_H1_M5] = s_m5
        setup_events[ALERT_TF_H1_M5] = e_m5

    return SymbolSnapshot(
        symbol=symbol,
        price=price,
        global_result=global_zone,
        local_result=local_zone,
        global_trend=_trend_from_event(g.last_structure_event),
        local_trend=_trend_from_event(l.last_structure_event),
        global_closes=[b.close for b in d1_closed[-SPARKLINE_LEN:]],
        local_closes=[b.close for b in h1_closed[-SPARKLINE_LEN:]],
        confirmation_bars={
            ALERT_TF_D1: h1_closed[-FVG_LOOKBACK:],
            ALERT_TF_H1: m15_closed[-FVG_LOOKBACK:],
            **({ALERT_TF_H1_M5: m5_closed[-FVG_LOOKBACK:]} if needs_m5 else {}),
        },
        htf_bars={
            ALERT_TF_D1: list(d1_closed),
            ALERT_TF_H1: list(h1_closed),
            **({ALERT_TF_H1_M5: list(h1_closed)} if needs_m5 else {}),
        },
        setup_states=setup_states,
        setup_events=setup_events,
    )
