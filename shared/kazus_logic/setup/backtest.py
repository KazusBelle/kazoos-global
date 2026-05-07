"""
Minimal CLI runner for the long-only INV/CRE/STB detector.

This is a lightweight diagnostic, not a full historical backtest. It
fetches one (HTF, LTF) pair from Binance, runs the engine to determine
the current OTE state on the HTF, and replays the detector across the
LTF window — printing every event the detector would have fired had
it been running live.

A full multi-symbol historical backtest (replaying every OTE window
the HTF saw across a date range) belongs in Etap 2, alongside DB
persistence of SetupState.

Usage:
    PYTHONPATH=shared python3 -m kazus_logic.setup.backtest \\
        --symbol BTCUSDT --htf-pair local --htf-limit 600 --ltf-limit 600
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List

from ..binance import BinanceFuturesClient
from ..engine import Bar, KazusGlobalEngine, KazusLocalEngine, ZoneResult
from . import SetupEvent, SetupState, detect_setup


HTF_PAIRS = {
    # name -> (htf_interval, ltf_interval, engine_factory)
    "global": ("1d", "1h", lambda: KazusGlobalEngine()),
    "local": ("1h", "15m", lambda: KazusLocalEngine()),
    "local-m5": ("1h", "5m", lambda: KazusLocalEngine()),
}


async def _run(args: argparse.Namespace) -> int:
    if args.htf_pair not in HTF_PAIRS:
        print(f"Unknown htf-pair: {args.htf_pair}", file=sys.stderr)
        return 2
    htf_iv, ltf_iv, make_engine = HTF_PAIRS[args.htf_pair]

    client = BinanceFuturesClient()
    try:
        htf_bars = await client.klines(args.symbol, htf_iv, limit=args.htf_limit)
        ltf_bars = await client.klines(args.symbol, ltf_iv, limit=args.ltf_limit)
    finally:
        await client.close()

    htf_closed = htf_bars[:-1] if len(htf_bars) > 1 else htf_bars
    ltf_closed = ltf_bars[:-1] if len(ltf_bars) > 1 else ltf_bars
    if not htf_closed or not ltf_closed:
        print("Not enough bars from Binance.", file=sys.stderr)
        return 1

    engine = make_engine()
    for b in htf_closed:
        engine.feed(b)
    last_price = ltf_bars[-1].close
    zone = engine.snapshot(last_price)

    print(_zone_summary(args.symbol, htf_iv, ltf_iv, zone))
    if not zone.in_ote or zone.direction != "bullish":
        print("Not in a bullish OTE. Detector idle.")
        return 0

    print(f"Replaying {len(ltf_closed)} {ltf_iv} bars through the detector...\n")
    prev: SetupState | None = None
    fired: List[SetupEvent] = []
    for n in range(1, len(ltf_closed) + 1):
        state, events = detect_setup(
            zone, ltf_closed[:n], prev,
            symbol=args.symbol, timeframe=ltf_iv,
        )
        for ev in events:
            fired.append(ev)
            print(_event_line(ev))
        prev = state

    print(f"\nFinal state: {prev.state if prev else 'NO'}")
    print(f"Total events fired: {len(fired)}")
    return 0


def _zone_summary(symbol: str, htf: str, ltf: str, zone: ZoneResult) -> str:
    return (
        f"{symbol} {htf}/{ltf} | zone={zone.zone} in_ote={zone.in_ote} "
        f"dir={zone.direction} retr={zone.retracement} "
        f"ote=[{zone.ote_low_price}, {zone.ote_high_price}]"
    )


def _event_line(ev: SetupEvent) -> str:
    return (
        f"  {ev.kind:<3} ts={ev.trigger_ts} fvg={ev.fvg.kind} "
        f"top={ev.fvg.top:.6g} bottom={ev.fvg.bottom:.6g} "
        f"swing_low={ev.swing_low:.6g} id={ev.event_id}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.strip())
    p.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    p.add_argument(
        "--htf-pair", default="local",
        choices=sorted(HTF_PAIRS.keys()),
        help="HTF/LTF pair: 'global' (D1/H1), 'local' (H1/M15), 'local-m5' (H1/M5)",
    )
    p.add_argument("--htf-limit", type=int, default=600)
    p.add_argument("--ltf-limit", type=int, default=600)
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
