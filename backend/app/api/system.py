from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..db.base import get_db
from ..models.models import ServerMetric
from .deps import get_current_user

router = APIRouter(tags=["system"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ServerMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    swap_percent: Optional[float] = None
    disk_percent: Optional[float] = None
    net_connections: Optional[int] = None


class ServerMetricsResponse(BaseModel):
    points: list[ServerMetricOut]
    latest: Optional[ServerMetricOut] = None


@router.get("/system/metrics", response_model=ServerMetricsResponse)
def server_metrics(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=2000, ge=24, le=12000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    cutoff = _utcnow() - timedelta(hours=hours)
    rows = (
        db.query(ServerMetric)
        .filter(ServerMetric.created_at >= cutoff)
        .order_by(ServerMetric.created_at.desc())
        .limit(limit)
        .all()
    )
    points = [ServerMetricOut.model_validate(row) for row in reversed(rows)]
    return ServerMetricsResponse(
        points=points,
        latest=points[-1] if points else None,
    )
