"""Binance USDT-M Futures adapter for the cross-exchange comparator.

Three small REST calls per symbol — depth (best bid/ask), funding,
open interest. Same data we already poll inside the liquidity loop;
duplicated here so the comparator can be invoked ad-hoc (e.g. when a
user opens the detail modal) without going through the worker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .base import ExchangeSnapshot

logger = logging.getLogger("kazus.liquidity.exchanges.binance")
BASE = "https://fapi.binance.com"


@dataclass
class BinanceExchange:
    name: str = "binance"

    async def fetch_snapshot(
        self, client: httpx.AsyncClient, symbol: str
    ) -> Optional[ExchangeSnapshot]:
        symbol_u = symbol.upper()
        try:
            depth_r, prem_r, oi_r = await asyncio.gather(
                client.get(f"{BASE}/fapi/v1/depth", params={"symbol": symbol_u, "limit": 5}),
                client.get(f"{BASE}/fapi/v1/premiumIndex", params={"symbol": symbol_u}),
                client.get(f"{BASE}/fapi/v1/openInterest", params={"symbol": symbol_u}),
            )
        except httpx.HTTPError as exc:
            logger.warning("binance crossex fetch failed for %s: %s", symbol_u, exc)
            return None

        if not all(r.is_success for r in (depth_r, prem_r, oi_r)):
            return None

        depth = depth_r.json()
        prem = prem_r.json()
        oi = oi_r.json()
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            return None
        if best_bid <= 0 or best_ask <= 0:
            return None
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid if mid > 0 else None

        funding = None
        try:
            funding = float(prem.get("lastFundingRate"))
        except (TypeError, ValueError):
            pass

        oi_usd = None
        try:
            oi_contracts = float(oi.get("openInterest"))
            oi_usd = oi_contracts * mid
        except (TypeError, ValueError):
            pass

        return ExchangeSnapshot(
            exchange=self.name,
            symbol=symbol_u,
            funding_rate=funding,
            open_interest_usd=oi_usd,
            spread_fraction=spread,
            mid_price=mid,
            ts_ms=int(time.time() * 1000),
        )
