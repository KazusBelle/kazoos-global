import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.base import get_db
from ..models.models import AlertState, Coin, Snapshot
from ..schemas.schemas import CallRequest, CoinIn, CoinOut, PinMoveRequest
from .deps import get_current_user

router = APIRouter(prefix="/coins", tags=["coins"])


VALID_CALL_TAGS = {"vanga", "voldemar", "makiavelli", "me"}
FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SYMBOL_CACHE_TTL_SEC = 60 * 60
_symbol_cache: tuple[float, list[str]] = (0, [])


@router.get("", response_model=list[CoinOut])
def list_coins(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Coin).order_by(Coin.symbol.asc()).all()


@router.post("", response_model=CoinOut)
def add_coin(body: CoinIn, db: Session = Depends(get_db), _=Depends(get_current_user)):
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="empty symbol")
    exists = db.query(Coin).filter(Coin.symbol == symbol).first()
    if exists:
        return exists
    coin = Coin(symbol=symbol, is_active=True)
    db.add(coin)
    db.commit()
    db.refresh(coin)
    return coin


@router.get("/suggestions", response_model=list[str])
async def suggest_coins(
    q: str = Query("", max_length=32),
    limit: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    symbols = await _available_symbols(db)
    needle = q.strip().upper()
    if needle and not needle.endswith("USDT"):
        loose = needle
        with_suffix = f"{needle}USDT"
    else:
        loose = needle[:-4] if needle.endswith("USDT") else needle
        with_suffix = needle

    def rank(symbol: str) -> tuple[int, str]:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if with_suffix and symbol == with_suffix:
            return (0, symbol)
        if loose and base == loose:
            return (1, symbol)
        if with_suffix and symbol.startswith(with_suffix):
            return (2, symbol)
        if loose and base.startswith(loose):
            return (3, symbol)
        if needle and needle in symbol:
            return (4, symbol)
        return (5, symbol)

    filtered = [s for s in symbols if not needle or rank(s)[0] < 5]
    return sorted(filtered, key=rank)[:limit]


@router.delete("/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_coin(
    symbol: str, db: Session = Depends(get_db), _=Depends(get_current_user)
):
    symbol = symbol.strip().upper()
    coin = db.query(Coin).filter(Coin.symbol == symbol).first()
    if coin is None:
        raise HTTPException(status_code=404, detail="not found")
    db.query(Snapshot).filter(Snapshot.symbol == symbol).delete()
    db.query(AlertState).filter(AlertState.symbol == symbol).delete()
    db.delete(coin)
    db.commit()
    _compact_pins(db)
    db.commit()
    return None


@router.post("/{symbol}/pin", response_model=CoinOut)
def toggle_pin(
    symbol: str, db: Session = Depends(get_db), _=Depends(get_current_user)
):
    symbol = symbol.strip().upper()
    coin = db.query(Coin).filter(Coin.symbol == symbol).first()
    if coin is None:
        raise HTTPException(status_code=404, detail="not found")

    if coin.pinned_order is None:
        # pin — put at the bottom of the pinned list
        max_order = db.query(func.max(Coin.pinned_order)).scalar()
        coin.pinned_order = 0 if max_order is None else max_order + 1
    else:
        # unpin & compact the remaining orders
        coin.pinned_order = None
        db.flush()
        _compact_pins(db)
    db.commit()
    db.refresh(coin)
    return coin


@router.post("/{symbol}/pin/move", response_model=list[CoinOut])
def move_pin(
    symbol: str,
    body: PinMoveRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if body.direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be up|down")

    symbol = symbol.strip().upper()
    coin = (
        db.query(Coin)
        .filter(Coin.symbol == symbol, Coin.pinned_order.isnot(None))
        .first()
    )
    if coin is None:
        raise HTTPException(status_code=404, detail="coin is not pinned")

    order_q = db.query(Coin).filter(Coin.pinned_order.isnot(None))
    if body.direction == "up":
        # swap with the pinned coin just above (smaller order)
        neighbor = (
            order_q.filter(Coin.pinned_order < coin.pinned_order)
            .order_by(Coin.pinned_order.desc())
            .first()
        )
    else:
        neighbor = (
            order_q.filter(Coin.pinned_order > coin.pinned_order)
            .order_by(Coin.pinned_order.asc())
            .first()
        )
    if neighbor is None:
        # already at the edge; no-op
        return db.query(Coin).order_by(Coin.symbol.asc()).all()

    coin.pinned_order, neighbor.pinned_order = neighbor.pinned_order, coin.pinned_order
    db.commit()
    return db.query(Coin).order_by(Coin.symbol.asc()).all()


@router.post("/{symbol}/call", response_model=CoinOut)
def set_call(
    symbol: str,
    body: CallRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    symbol = symbol.strip().upper()
    coin = db.query(Coin).filter(Coin.symbol == symbol).first()
    if coin is None:
        raise HTTPException(status_code=404, detail="not found")
    tag = body.tag.strip().lower() if body.tag else None
    if tag is not None and tag not in VALID_CALL_TAGS:
        raise HTTPException(
            status_code=400,
            detail=f"tag must be one of: {sorted(VALID_CALL_TAGS)} or null",
        )
    coin.call_tag = tag
    coin.call_note = (body.note.strip() if body.note else None) or None
    db.commit()
    db.refresh(coin)
    return coin


def _compact_pins(db: Session) -> None:
    """Rewrite pinned_order values to be contiguous 0..N-1 preserving order."""
    pinned = (
        db.query(Coin)
        .filter(Coin.pinned_order.isnot(None))
        .order_by(Coin.pinned_order.asc())
        .all()
    )
    for idx, c in enumerate(pinned):
        c.pinned_order = idx


async def _available_symbols(db: Session) -> list[str]:
    global _symbol_cache

    now = time.time()
    if _symbol_cache[1] and now - _symbol_cache[0] < SYMBOL_CACHE_TTL_SEC:
        return _symbol_cache[1]

    symbols: set[str] = {
        c.symbol.upper()
        for c in db.query(Coin.symbol).order_by(Coin.symbol.asc()).all()
        if c.symbol
    }
    settings = get_settings()
    symbols.update(
        s.strip().upper()
        for s in settings.default_coins.split(",")
        if s.strip()
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(FUTURES_EXCHANGE_INFO)
            res.raise_for_status()
            payload = res.json()
        symbols.update(
            item["symbol"].upper()
            for item in payload.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
        )
    except Exception:
        pass

    _symbol_cache = (now, sorted(symbols))
    return _symbol_cache[1]
