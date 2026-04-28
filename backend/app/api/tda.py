import json

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.base import get_db
from ..models.models import User, UserTDAState
from ..schemas.schemas import TDAStateIn, TDAStateOut, TDAStatePatchIn
from .deps import get_current_user

router = APIRouter(prefix="/tda", tags=["tda"])


def _ensure_tda_table(db: Session) -> None:
    # Defensive bootstrap: some environments may run with stale DB schema.
    # Keep TDA endpoint self-healing instead of returning 500.
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_tda_states (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                coins_json TEXT,
                data_json TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_user_tda_states_user_id
            ON user_tda_states (user_id)
            """
        )
    )
    db.commit()


def _safe_get_state(db: Session, user_id: int) -> UserTDAState | None:
    try:
        return db.query(UserTDAState).filter(UserTDAState.user_id == user_id).first()
    except Exception:
        db.rollback()
        _ensure_tda_table(db)
        return db.query(UserTDAState).filter(UserTDAState.user_id == user_id).first()


def _normalize_coins(coins: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in coins:
        symbol = str(item).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _normalize_data(raw: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for symbol, cols in raw.items():
        sym = str(symbol).strip().upper()
        if not sym or not isinstance(cols, dict):
            continue
        next_cols: dict[str, str] = {}
        for col, value in cols.items():
            next_cols[str(col)] = str(value)
        out[sym] = next_cols
    return out


def _normalize_photos(raw: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for symbol, value in raw.items():
        sym = str(symbol).strip().upper()
        photo = str(value).strip()
        if not sym or not photo:
            continue
        out[sym] = photo
    return out


def _parse_coins(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        return _normalize_coins([str(item) for item in parsed])
    except (ValueError, TypeError):
        return []


def _decode_payload(raw: str | None) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    if not raw:
        return {}, {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}, {}
    if not isinstance(parsed, dict):
        return {}, {}

    # New shape: {"cells": {...}, "photos": {...}}
    if "cells" in parsed or "photos" in parsed:
        data = _normalize_data(parsed.get("cells") if isinstance(parsed.get("cells"), dict) else {})
        photos = _normalize_photos(parsed.get("photos") if isinstance(parsed.get("photos"), dict) else {})
        return data, photos

    # Legacy shape: plain table dict
    return _normalize_data(parsed), {}


def _encode_payload(data: dict[str, dict[str, str]], photos: dict[str, str]) -> str:
    return json.dumps({"cells": data, "photos": photos})


@router.get("/state", response_model=TDAStateOut)
def get_tda_state(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    state = _safe_get_state(db, user.id)
    if state is None:
        return TDAStateOut(coins=[], data={}, photos={})
    data, photos = _decode_payload(state.data_json)
    return TDAStateOut(
        coins=_parse_coins(state.coins_json),
        data=data,
        photos=photos,
    )


@router.put("/state", response_model=TDAStateOut)
def put_tda_state(
    body: TDAStateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    coins = _normalize_coins(body.coins)
    data = _normalize_data(body.data)
    photos = _normalize_photos(body.photos)

    state = _safe_get_state(db, user.id)
    if state is None:
        state = UserTDAState(user_id=user.id)
        db.add(state)

    state.coins_json = json.dumps(coins)
    state.data_json = _encode_payload(data, photos)
    db.commit()

    return TDAStateOut(coins=coins, data=data, photos=photos)


@router.patch("/state", response_model=TDAStateOut)
def patch_tda_state(
    body: TDAStatePatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    state = _safe_get_state(db, user.id)
    if state is None:
        state = UserTDAState(user_id=user.id)
        db.add(state)

    existing_coins = _parse_coins(state.coins_json)
    existing_data, existing_photos = _decode_payload(state.data_json)

    coins = _normalize_coins(body.coins) if body.coins is not None else existing_coins
    data = _normalize_data(body.data) if body.data is not None else existing_data
    photos = _normalize_photos(body.photos) if body.photos is not None else existing_photos

    state.coins_json = json.dumps(coins)
    state.data_json = _encode_payload(data, photos)
    db.commit()

    return TDAStateOut(coins=coins, data=data, photos=photos)
