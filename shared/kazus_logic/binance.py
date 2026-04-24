"""Thin async client for Binance USDT-M Futures klines."""

from __future__ import annotations

from typing import List

import httpx

from .engine import Bar

FUTURES_BASE = "https://fapi.binance.com"


class BinanceFuturesClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=FUTURES_BASE, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def exchange_info_symbols(self) -> List[str]:
        r = await self._client.get("/fapi/v1/exchangeInfo")
        r.raise_for_status()
        data = r.json()
        return [
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ]

    async def klines(
        self, symbol: str, interval: str, limit: int = 500
    ) -> List[Bar]:
        r = await self._client.get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        rows = r.json()
        bars: List[Bar] = []
        for row in rows:
            bars.append(
                Bar(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                )
            )
        return bars

    async def mark_price(self, symbol: str) -> float:
        r = await self._client.get(
            "/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        r.raise_for_status()
        return float(r.json()["markPrice"])
