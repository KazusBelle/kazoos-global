import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import psutil
from sqlalchemy import delete

from ..core.config import get_settings
from ..db.base import SessionLocal
from ..models.models import ServerMetric

logger = logging.getLogger("kazus.server_metrics")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def collect_server_metric() -> ServerMetric:
    load = os.getloadavg()
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    try:
        net_connections = len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        net_connections = None

    return ServerMetric(
        created_at=_utcnow(),
        load_1m=load[0],
        load_5m=load[1],
        load_15m=load[2],
        cpu_percent=psutil.cpu_percent(interval=None),
        memory_percent=memory.percent,
        swap_percent=swap.percent,
        disk_percent=disk.percent,
        net_connections=net_connections,
    )


def save_server_metric() -> None:
    settings = get_settings()
    cutoff = _utcnow() - timedelta(hours=settings.server_metrics_retention_hours)
    with SessionLocal() as db:
        db.add(collect_server_metric())
        db.execute(delete(ServerMetric).where(ServerMetric.created_at < cutoff))
        db.commit()


async def run_server_metrics_collector(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = max(10, settings.server_metrics_interval_sec)
    psutil.cpu_percent(interval=None)

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(save_server_metric)
        except Exception as exc:
            logger.exception("server metrics collection failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
