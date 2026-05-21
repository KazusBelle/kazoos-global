"""Symbol universe for the liquidity polling worker.

Returns the intersection of CoinGecko's top-N (by market cap) with the
Binance Futures USDT-M perpetual list, so we only track symbols the
exchange can actually quote. Cached briefly to avoid hitting CoinGecko
on every polling cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import httpx

logger = logging.getLogger("kazus.liquidity.universe")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
COINGECKO_PER_PAGE = 250
UNIVERSE_TTL_S = 600  # CoinGecko top-N is stable enough — refresh every 10 min


@dataclass
class UniverseEntry:
    binance_symbol: str       # "BTCUSDT", "1000PEPEUSDT"
    rank: int                 # CoinGecko market-cap rank
    name: str
    coingecko_symbol: str     # "BTC", "PEPE"


_cache: tuple[float, list[UniverseEntry]] | None = None
_cache_lock = asyncio.Lock()


async def get_universe(limit: int = 100) -> List[UniverseEntry]:
    """Return up to `limit` symbols, ordered by CoinGecko market-cap rank.

    Caches the full slice for UNIVERSE_TTL_S. The function is safe to
    call concurrently — the lock serializes the upstream fetch.
    """
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < UNIVERSE_TTL_S:
        return _cache[1][:limit]

    async with _cache_lock:
        if _cache and now - _cache[0] < UNIVERSE_TTL_S:
            return _cache[1][:limit]

        # Fetch enough pages to cover the largest expected limit (500).
        pages = max(1, (max(limit, 500) + COINGECKO_PER_PAGE - 1) // COINGECKO_PER_PAGE)
        cg_rows: list[dict] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                for page in range(1, pages + 1):
                    r = await client.get(
                        f"{COINGECKO_BASE}/coins/markets",
                        params={
                            "vs_currency": "usd",
                            "order": "market_cap_desc",
                            "per_page": COINGECKO_PER_PAGE,
                            "page": page,
                            "sparkline": "false",
                        },
                    )
                    r.raise_for_status()
                    cg_rows.extend(r.json())

                r = await client.get(BINANCE_FUTURES_EXCHANGE_INFO)
                r.raise_for_status()
                ex_info = r.json()
            except httpx.HTTPError as exc:
                logger.warning("universe upstream failed: %s", exc)
                return [] if _cache is None else _cache[1][:limit]

        bases: dict[str, str] = {}
        for s in ex_info.get("symbols", []):
            if (
                s.get("contractType") != "PERPETUAL"
                or s.get("quoteAsset") != "USDT"
                or s.get("status") != "TRADING"
            ):
                continue
            base = s.get("baseAsset", "").upper()
            if base.startswith("1000"):
                base = base[4:]
            if base:
                bases.setdefault(base, s["symbol"])

        out: list[UniverseEntry] = []
        for item in cg_rows:
            sym = (item.get("symbol") or "").upper()
            binance_symbol = bases.get(sym)
            if not binance_symbol:
                continue
            out.append(
                UniverseEntry(
                    binance_symbol=binance_symbol,
                    rank=int(item.get("market_cap_rank") or len(out) + 1),
                    name=item.get("name") or sym,
                    coingecko_symbol=sym,
                )
            )

        _cache = (now, out)
        return out[:limit]


def invalidate_cache() -> None:
    global _cache
    _cache = None
