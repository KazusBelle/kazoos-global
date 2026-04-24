from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.base import get_db
from ..models.models import Coin, Snapshot, SystemStatus
from ..schemas.schemas import DashboardResponse, DashboardRow, SnapshotOut
from .deps import get_current_user

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse, response_model_by_alias=True)
def dashboard(db: Session = Depends(get_db), _=Depends(get_current_user)):
    coins = db.query(Coin).order_by(Coin.symbol.asc()).all()
    snaps = {
        (s.symbol, s.timeframe): s
        for s in db.query(Snapshot).all()
    }

    rows = []
    total = 0
    ote_count = 0
    dic_count = 0
    pre_count = 0
    for coin in coins:
        g = snaps.get((coin.symbol, "D1"))
        l = snaps.get((coin.symbol, "H1"))
        rows.append(
            DashboardRow.model_validate(
                {
                    "symbol": coin.symbol,
                    "price": (l.price if l else (g.price if g else None)),
                    "global": SnapshotOut.model_validate(g) if g else None,
                    "local": SnapshotOut.model_validate(l) if l else None,
                }
            )
        )

        for s in (g, l):
            if s is None:
                continue
            total += 1
            if s.in_ote:
                ote_count += 1
            if s.zone == "discount":
                dic_count += 1
            elif s.zone == "premium":
                pre_count += 1

    sys_status = db.query(SystemStatus).filter(SystemStatus.id == 1).first()
    return DashboardResponse(
        rows=rows,
        totals={
            "total": total,
            "ote": ote_count,
            "discount": dic_count,
            "premium": pre_count,
        },
        last_refresh_at=sys_status.last_refresh_at if sys_status else None,
        last_error=sys_status.last_error if sys_status else None,
    )
