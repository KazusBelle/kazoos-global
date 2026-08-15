import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..core.security import create_access_token, verify_password
from ..db.base import get_db
from ..models.models import User
from ..schemas.schemas import LoginRequest, TokenResponse

logger = logging.getLogger("kazus.backend.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Login is reachable from the public internet through both the nginx container
# (:80) and the pm2 vite proxy (:8080) — binding the backend port to loopback
# did not change that. Until now nothing recorded a failed attempt, so a
# password-guessing run was indistinguishable from a typo, and there was no way
# to answer "did anyone get in" after the JWT secret turned out to be public.
#
# In-process counter on purpose: one backend instance, one operator. A shared
# store or a rate-limit library would add moving parts to protect a single door.
_FAIL_WINDOW_S = 300
_FAIL_LIMIT = 10
_MAX_TRACKED_IPS = 2048
_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Caller address, honouring the proxy chain.

    nginx forwards X-Real-IP and X-Forwarded-For. The vite dev server does NOT
    (no `xfwd`), so attempts arriving through :8080 report the proxy's address
    rather than the visitor's — worth remembering when reading these logs.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


def _guard(ip: str) -> None:
    now = time.time()
    recent = [t for t in _failures.get(ip, ()) if now - t < _FAIL_WINDOW_S]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    if len(recent) >= _FAIL_LIMIT:
        logger.warning(
            "login blocked ip=%s reason=rate_limit fails=%d window=%ds",
            ip, len(recent), _FAIL_WINDOW_S,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts, try again later",
            headers={"Retry-After": str(_FAIL_WINDOW_S)},
        )


def _record_failure(ip: str, username: str) -> None:
    now = time.time()
    # Bound the map so a spray across forged IPs cannot grow it without limit.
    if len(_failures) >= _MAX_TRACKED_IPS and ip not in _failures:
        stale = [k for k, v in _failures.items() if not v or now - v[-1] > _FAIL_WINDOW_S]
        for k in stale[:256]:
            _failures.pop(k, None)
    _failures.setdefault(ip, []).append(now)
    logger.warning(
        "login failed ip=%s username=%r fails_in_window=%d",
        ip, username, len(_failures[ip]),
    )


def _authenticate(db: Session, request: Request, username: str, password: str) -> str:
    ip = _client_ip(request)
    _guard(ip)
    user = db.query(User).filter(User.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        _record_failure(ip, username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    _failures.pop(ip, None)
    logger.info("login ok ip=%s username=%r", ip, username)
    return create_access_token(user.username)


@router.post("/login", response_model=TokenResponse)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    return TokenResponse(
        access_token=_authenticate(db, request, form.username, form.password)
    )


@router.post("/login-json", response_model=TokenResponse)
def login_json(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    return TokenResponse(
        access_token=_authenticate(db, request, body.username, body.password)
    )
