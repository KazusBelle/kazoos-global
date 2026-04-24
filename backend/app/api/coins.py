from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db.base import get_db
from ..models.models import Coin, Snapshot, AlertState
from ..schemas.schemas import CoinIn, CoinOut
from .deps import get_current_user

router = APIRouter(prefix="/coins", tags=["coins"])


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
    return None
