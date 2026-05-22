"""Cross-exchange validation foundation.

Phase 6 stops at a single comparison venue (Bybit) — but the public API
is built so adding OKX, Deribit, or whoever later is just one more
Exchange subclass and one more line in REGISTRY. Each exchange exposes
the same three fetchers (funding, OI, top-level spread) so the
divergence module can compare apples to apples.
"""

from .base import Exchange, ExchangeSnapshot
from .binance import BinanceExchange
from .bybit import BybitExchange

REGISTRY: dict[str, Exchange] = {
    "binance": BinanceExchange(),
    "bybit": BybitExchange(),
}

__all__ = ["Exchange", "ExchangeSnapshot", "REGISTRY"]
