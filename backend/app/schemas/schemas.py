from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CoinIn(BaseModel):
    symbol: str


class CoinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    is_active: bool
    pinned_order: Optional[int] = None
    call_tag: Optional[str] = None
    call_note: Optional[str] = None


class PinMoveRequest(BaseModel):
    direction: str  # "up" | "down"


class CallRequest(BaseModel):
    tag: Optional[str] = None     # one of: vanga, voldemar, makiavelli, me, None
    note: Optional[str] = None


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    timeframe: str
    price: Optional[float] = None
    direction: str
    zone: str
    in_ote: bool
    setup: str
    retracement: Optional[float] = None
    fib_low: Optional[float] = None
    fib_high: Optional[float] = None
    ote_low_price: Optional[float] = None
    ote_high_price: Optional[float] = None
    trend: str
    closes: List[float] = []
    updated_at: datetime


class AlertEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timeframe: str
    message: str
    created_at: datetime


class DashboardRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    price: Optional[float] = None
    pinned_order: Optional[int] = None
    call_tag: Optional[str] = None
    call_note: Optional[str] = None
    global_: Optional[SnapshotOut] = Field(default=None, alias="global")
    local: Optional[SnapshotOut] = None


class DashboardResponse(BaseModel):
    rows: List[DashboardRow]
    totals: dict
    recent_alerts: List[AlertEventOut] = []
    last_refresh_at: Optional[datetime] = None
    last_error: Optional[str] = None


class TDAStateIn(BaseModel):
    coins: List[str] = []
    data: dict[str, dict[str, str]] = {}
    photos: dict[str, str] = {}


class TDAStateOut(BaseModel):
    coins: List[str] = []
    data: dict[str, dict[str, str]] = {}
    photos: dict[str, str] = {}


class TDAStatePatchIn(BaseModel):
    coins: Optional[List[str]] = None
    data: Optional[dict[str, dict[str, str]]] = None
    photos: Optional[dict[str, str]] = None
