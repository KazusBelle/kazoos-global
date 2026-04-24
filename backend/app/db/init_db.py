from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.security import hash_password
from ..models.models import User, Coin, SystemStatus
from .base import Base, engine, SessionLocal


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def seed_initial_data() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        _ensure_admin(db, settings.admin_username, settings.admin_password)
        _ensure_default_coins(db, settings.default_coins)
        _ensure_status_row(db)
        db.commit()


def _ensure_admin(db: Session, username: str, password: str) -> None:
    if db.query(User).count() > 0:
        return
    admin = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=True,
    )
    db.add(admin)


def _ensure_default_coins(db: Session, csv: str) -> None:
    if db.query(Coin).count() > 0:
        return
    for sym in [s.strip().upper() for s in csv.split(",") if s.strip()]:
        db.add(Coin(symbol=sym, is_active=True))


def _ensure_status_row(db: Session) -> None:
    if db.query(SystemStatus).filter(SystemStatus.id == 1).first() is None:
        db.add(SystemStatus(id=1))
