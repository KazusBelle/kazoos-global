"""Liquidity screener — top-N coins by market cap, restricted to symbols
that also trade on Binance USDT-M Futures.

CoinGecko is the universe source (free public API). Binance Futures
exchange info gives us the tradable set. We intersect the two so the LIQ
table only ever shows coins the rest of the screener can actually
analyze. Both upstream calls are cached briefly to avoid rate-limiting
on rapid page reloads — the data updates on the order of minutes, not
seconds, so a short TTL is fine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..models.models import User
from .deps import get_current_user

logger = logging.getLogger("kazus.backend.liquidity")
router = APIRouter(prefix="/liquidity", tags=["liquidity"])

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ALLOWED_LIMITS = (100, 250, 500)
PAGE_SIZE = 250  # CoinGecko per_page max
CACHE_TTL_S = 300  # 5 minutes


class LiqRow(BaseModel):
    rank: int
    coingecko_symbol: str         # e.g. "BTC", "PEPE"
    binance_symbol: str           # e.g. "BTCUSDT", "1000PEPEUSDT"
    name: str
    market_cap: Optional[float]
    volume_24h: Optional[float]
    price: Optional[float]
    change_24h_pct: Optional[float]
    image: Optional[str]


class LiqResponse(BaseModel):
    limit: int
    rows: List[LiqRow]
    fetched_at: float


# Module-level cache. Single-process worker, so a plain dict is enough —
# no need for redis here. Keyed by (source, limit_or_none).
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()


async def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    async with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = await fn()
        _cache[key] = (now, value)
        return value


async def _fetch_binance_bases() -> dict[str, str]:
    """Return mapping `normalized_base -> binance_symbol`.

    Binance prefixes ultra-cheap tokens with "1000" (1000PEPEUSDT, etc.).
    The CoinGecko symbol for the same coin is just "PEPE", so we
    normalize both sides by stripping that prefix on the Binance side.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(BINANCE_FUTURES_EXCHANGE_INFO)
        r.raise_for_status()
        data = r.json()
    mapping: dict[str, str] = {}
    for s in data.get("symbols", []):
        if (
            s.get("contractType") != "PERPETUAL"
            or s.get("quoteAsset") != "USDT"
            or s.get("status") != "TRADING"
        ):
            continue
        symbol: str = s["symbol"]                  # "1000PEPEUSDT"
        base: str = s.get("baseAsset", "").upper()  # "1000PEPE"
        if base.startswith("1000"):
            base = base[4:]
        if not base:
            continue
        # First write wins — Binance shouldn't list duplicate bases anyway.
        mapping.setdefault(base, symbol)
    return mapping


async def _fetch_coingecko_top(limit: int) -> List[dict]:
    """Fetch the first `limit` coins by market cap from CoinGecko.

    `limit` is one of ALLOWED_LIMITS. CoinGecko's per_page maxes out at
    250, so 500 needs two pages.
    """
    pages = 1 if limit <= PAGE_SIZE else 2
    results: List[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for page in range(1, pages + 1):
            r = await client.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                    "sparkline": "false",
                },
            )
            r.raise_for_status()
            results.extend(r.json())
            if len(results) >= limit:
                break
    return results[:limit]


@router.get("/top", response_model=LiqResponse)
async def get_top(
    limit: int = Query(100),
    _user: User = Depends(get_current_user),
) -> LiqResponse:
    if limit not in ALLOWED_LIMITS:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be one of {ALLOWED_LIMITS}",
        )

    try:
        cg = await _cached(
            f"cg:{limit}", CACHE_TTL_S, lambda: _fetch_coingecko_top(limit)
        )
        binance_bases = await _cached(
            "binance:bases", CACHE_TTL_S, _fetch_binance_bases
        )
    except httpx.HTTPError as exc:
        logger.warning("liquidity upstream failed: %s", exc)
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    rows: List[LiqRow] = []
    for item in cg:
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        binance_symbol = binance_bases.get(sym)
        if not binance_symbol:
            continue
        rows.append(
            LiqRow(
                rank=int(item.get("market_cap_rank") or len(rows) + 1),
                coingecko_symbol=sym,
                binance_symbol=binance_symbol,
                name=item.get("name") or sym,
                market_cap=item.get("market_cap"),
                volume_24h=item.get("total_volume"),
                price=item.get("current_price"),
                change_24h_pct=item.get("price_change_percentage_24h"),
                image=item.get("image"),
            )
        )

    return LiqResponse(limit=limit, rows=rows, fetched_at=time.time())
