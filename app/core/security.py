"""Security V1.0 — JWT + 3 roles (super_admin, teacher, student)."""
from datetime import datetime, timedelta, timezone as tz
from typing import Annotated
from dataclasses import dataclass
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import settings

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)


def hash_password(p: str) -> str:
    return pwd_ctx.hash(p)


def verify_password(p: str, h: str) -> bool:
    try:
        return pwd_ctx.verify(p, h)
    except Exception:
        return False


def create_access_token(uid: str, role: str) -> str:
    return jwt.encode({
        "sub": uid, "role": role, "type": "access",
        "exp": datetime.now(tz.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(uid: str) -> str:
    return jwt.encode({
        "sub": uid, "type": "refresh",
        "exp": datetime.now(tz.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    }, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expirado")
    except Exception:
        raise HTTPException(401, "Token inválido")


@dataclass
class CurrentUser:
    user_id: str
    role: str


async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentUser:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta token de autenticación")
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise HTTPException(401, "Se requiere access token")
    return CurrentUser(user_id=payload["sub"], role=payload.get("role", "student"))


def require_admin(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if user.role != "super_admin":
        raise HTTPException(403, "Solo super_admin puede realizar esta acción")
    return user


def require_teacher_or_admin(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if user.role not in ("super_admin", "teacher"):
        raise HTTPException(403, "Requiere rol profesor o admin")
    return user
