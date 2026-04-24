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
    updated_at: datetime


class DashboardRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    price: Optional[float] = None
    global_: Optional[SnapshotOut] = Field(default=None, alias="global")
    local: Optional[SnapshotOut] = None


class DashboardResponse(BaseModel):
    rows: List[DashboardRow]
    totals: dict
    last_refresh_at: Optional[datetime] = None
    last_error: Optional[str] = None
