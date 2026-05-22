"""Bybit USDT-perp adapter (cross-exchange validation foundation).

Bybit's v5 unified API returns ticker info (best bid/ask, funding,
OI all in one call) so this is a single HTTP request per symbol —
much cheaper than Binance's three-call pattern.

Symbol-mapping note: Bybit uses BASE+QUOTE without the "1000" prefix
that Binance uses for low-priced coins (Binance: "1000PEPEUSDT", Bybit:
"1000PEPEUSDT" too in many cases — Bybit also uses the 1000 prefix, so
in practice the symbol passes through unchanged for USDT perps. The
comparator passes the Binance-side symbol as-is; if a future mismatch
appears the mapping table belongs in this module, not in the caller.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .base import ExchangeSnapshot

logger = logging.getLogger("kazus.liquidity.exchanges.bybit")
BASE = "https://api.bybit.com"


@dataclass
class BybitExchange:
    name: str = "bybit"

    async def fetch_snapshot(
        self, client: httpx.AsyncClient, symbol: str
    ) -> Optional[ExchangeSnapshot]:
        symbol_u = symbol.upper()
        try:
            r = await client.get(
                f"{BASE}/v5/market/tickers",
                params={"category": "linear", "symbol": symbol_u},
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            logger.warning("bybit crossex fetch failed for %s: %s", symbol_u, exc)
            return None

        if data.get("retCode") != 0:
            return None
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            return None
        t = rows[0]
        try:
            bid = float(t.get("bid1Price") or 0)
            ask = float(t.get("ask1Price") or 0)
        except (TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else None

        funding = None
        try:
            funding = float(t.get("fundingRate"))
        except (TypeError, ValueError):
            pass

        oi_usd = None
        try:
            oi_value = float(t.get("openInterestValue"))   # already USD
            oi_usd = oi_value if oi_value > 0 else None
        except (TypeError, ValueError):
            pass
        # Fallback: openInterest is in contracts, multiply by mid.
        if oi_usd is None:
            try:
                oi_contracts = float(t.get("openInterest") or 0)
                if oi_contracts > 0:
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
