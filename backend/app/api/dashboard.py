import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.base import get_db
from ..models.models import AlertEvent, Coin, Snapshot, SystemStatus
from ..schemas.schemas import AlertEventOut, DashboardResponse, DashboardRow, SnapshotOut
from .deps import get_current_user

router = APIRouter(tags=["dashboard"])


def _snapshot_to_schema(s: Snapshot) -> SnapshotOut:
    data = {
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "price": s.price,
        "direction": s.direction,
        "zone": s.zone,
        "in_ote": s.in_ote,
        "setup": s.setup,
        "retracement": s.retracement,
        "fib_low": s.fib_low,
        "fib_high": s.fib_high,
        "ote_low_price": s.ote_low_price,
        "ote_high_price": s.ote_high_price,
        "trend": s.trend,
        "closes": _decode_closes(s.closes_json),
        "updated_at": s.updated_at,
    }
    return SnapshotOut.model_validate(data)


def _decode_closes(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [float(x) for x in parsed if x is not None]
    except (ValueError, TypeError):
        return []


@router.get("/dashboard", response_model=DashboardResponse, response_model_by_alias=True)
def dashboard(db: Session = Depends(get_db), _=Depends(get_current_user)):
    coins = db.query(Coin).order_by(Coin.symbol.asc()).all()
    snaps = {
        (s.symbol, s.timeframe): s
        for s in db.query(Snapshot).all()
    }
    recent_alerts = (
        db.query(AlertEvent)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(100)
        .all()
    )

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
                    "pinned_order": coin.pinned_order,
                    "call_tag": coin.call_tag,
                    "call_note": coin.call_note,
                    "global": _snapshot_to_schema(g) if g else None,
                    "local": _snapshot_to_schema(l) if l else None,
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
        recent_alerts=[AlertEventOut.model_validate(item) for item in recent_alerts],
        last_refresh_at=sys_status.last_refresh_at if sys_status else None,
        last_error=sys_status.last_error if sys_status else None,
    )
