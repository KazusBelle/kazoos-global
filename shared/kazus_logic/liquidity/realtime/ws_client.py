"""Binance Futures combined-stream WebSocket client.

One persistent connection. Streams are added / removed dynamically via
SUBSCRIBE / UNSUBSCRIBE control frames so a modal opening a new symbol
doesn't require reconnecting. Messages are exposed as an async iterator
of (stream_name, payload) tuples.

Reconnect is automatic with exponential backoff + jitter. After a
reconnect, the caller (SubscriptionManager) is expected to re-issue
SUBSCRIBE for the currently-desired streams — this client does NOT
remember its own subscription set across reconnects on purpose: the
manager already has the authoritative desired-set in memory, and
re-emitting it is simpler than tracking state in two places.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import AsyncIterator, Iterable, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("kazus.liquidity.ws")

FUTURES_WS_URL = "wss://fstream.binance.com/stream"
PING_INTERVAL_S = 30
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0


class FuturesWsClient:
    def __init__(self) -> None:
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._conn_id: int = 0
        self._req_id: int = 0
        self._connect_lock = asyncio.Lock()
        self._closed = False

    @property
    def conn_id(self) -> int:
        """Monotonic counter that bumps on every successful reconnect.
        Consumers compare against a remembered value to know when to
        re-issue SUBSCRIBE for their desired streams."""
        return self._conn_id

    @property
    def connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", True)

    async def _ensure_connected(self) -> websockets.WebSocketClientProtocol:
        async with self._connect_lock:
            if self._ws is not None and not getattr(self._ws, "closed", True):
                return self._ws
            backoff = RECONNECT_MIN_S
            while not self._closed:
                try:
                    self._ws = await websockets.connect(
                        FUTURES_WS_URL,
                        ping_interval=PING_INTERVAL_S,
                        ping_timeout=10,
                        close_timeout=5,
                        max_size=4 * 1024 * 1024,
                    )
                    self._conn_id += 1
                    logger.info("ws connected (conn_id=%d)", self._conn_id)
                    return self._ws
                except Exception as exc:  # noqa: BLE001
                    sleep_s = min(RECONNECT_MAX_S, backoff) + random.uniform(0, 1)
                    logger.warning(
                        "ws connect failed: %s; retrying in %.1fs",
                        exc, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    backoff = min(RECONNECT_MAX_S, backoff * 2)
            raise RuntimeError("ws client closed during connect")

    async def subscribe(self, streams: Iterable[str]) -> None:
        streams = list(streams)
        if not streams:
            return
        ws = await self._ensure_connected()
        self._req_id += 1
        await ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": streams,
            "id": self._req_id,
        }))
        logger.info("ws SUBSCRIBE %d streams (sample=%s)", len(streams), streams[:3])

    async def unsubscribe(self, streams: Iterable[str]) -> None:
        streams = list(streams)
        if not streams or not self.connected:
            return
        ws = self._ws
        if ws is None:
            return
        self._req_id += 1
        try:
            await ws.send(json.dumps({
                "method": "UNSUBSCRIBE",
                "params": streams,
                "id": self._req_id,
            }))
            logger.info("ws UNSUBSCRIBE %d streams (sample=%s)", len(streams), streams[:3])
        except ConnectionClosed:
            pass  # next iteration will reconnect; manager will re-derive

    async def messages(self) -> AsyncIterator[Tuple[str, dict]]:
        """Yield (stream_name, payload) for every data frame.

        Control-frame responses to SUBSCRIBE/UNSUBSCRIBE (which have no
        "stream" key) are silently consumed. On ConnectionClosed the
        iterator drops the current connection and lets the next call to
        subscribe() / messages() establish a new one — yielding NOTHING
        in the interim. The consumer must watch `conn_id` and re-issue
        SUBSCRIBE on detected bumps.
        """
        while not self._closed:
            ws = await self._ensure_connected()
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    stream = msg.get("stream")
                    data = msg.get("data")
                    if stream is None or data is None:
                        continue  # control frame
                    yield stream, data
            except ConnectionClosed as exc:
                logger.warning("ws connection closed: %s", exc)
                self._ws = None
                # backoff handled inside _ensure_connected on the next iteration
                await asyncio.sleep(RECONNECT_MIN_S + random.uniform(0, 1))
            except Exception as exc:  # noqa: BLE001
                logger.exception("ws read failed: %s", exc)
                self._ws = None
                await asyncio.sleep(RECONNECT_MIN_S + random.uniform(0, 2))

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
