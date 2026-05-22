"""Cross-exchange abstraction.

Each venue exposes the same three fetchers so the divergence layer can
diff them. Subclasses are stateless thin wrappers around the venue's
REST API — no caching here, callers cache one level up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

import httpx


@dataclass
class ExchangeSnapshot:
    """One snapshot of the comparison fields for a single symbol.

    Symbols are normalized at the comparator level (e.g. mapping
    "BTCUSDT" on Binance to "BTCUSDT" on Bybit) — each exchange just
    consumes the venue-native symbol passed to it.
    """
    exchange: str
    symbol: str
    funding_rate: Optional[float]    # current funding rate (fraction)
    open_interest_usd: Optional[float]
    spread_fraction: Optional[float] # (ask − bid) / mid
    mid_price: Optional[float]
    ts_ms: int


class Exchange(Protocol):
    name: str

    async def fetch_snapshot(
        self, client: httpx.AsyncClient, symbol: str
    ) -> Optional[ExchangeSnapshot]:
        ...
