import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .deps import get_current_user

logger = logging.getLogger("kazus.frontend")

router = APIRouter(tags=["frontend"])


class FrontendErrorIn(BaseModel):
    kind: str = Field(default="error")
    message: str
    source: Optional[str] = None
    stack: Optional[str] = None
    url: Optional[str] = None
    user_agent: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


@router.post("/frontend-errors")
def frontend_errors(payload: FrontendErrorIn, _=Depends(get_current_user)):
    logger.error(
        "frontend_error %s",
        json.dumps(payload.model_dump(mode="json"), ensure_ascii=True, default=str),
    )
    return {"ok": True}
