"""Auth — login, register, refresh, me."""
from datetime import datetime, timezone as tz
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr, Field

from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    get_current_user, CurrentUser,
)
from app.core.db import get_db
from app.models import User, Student, UserRole
from app.services.audit import log_action

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=2, max_length=160)
    phone: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    phone: Optional[str] = None
    role: str
    avatar_url: Optional[str] = None
    current_level_id: Optional[int] = None
    placement_done: Optional[bool] = None


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una cuenta con este email")
    user = User(
        email=body.email, password_hash=hash_password(body.password),
        full_name=body.full_name, phone=body.phone, role=UserRole.student,
    )
    db.add(user)
    await db.flush()
    db.add(Student(user_id=user.id))
    await log_action(db, user.id, "register", "auth", target_id=user.id)
    await db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email o contraseña incorrectos")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cuenta desactivada")
    user.last_login_at = datetime.now(tz.utc)
    await log_action(db, user.id, "login", "auth")
    await db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(401, "Se requiere refresh token")
    user = await db.get(User, payload["sub"])
    if not user:
        raise HTTPException(401, "Usuario no encontrado")
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[CurrentUser, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    out = UserOut(id=u.id, email=u.email, full_name=u.full_name, phone=u.phone,
                  role=u.role.value, avatar_url=u.avatar_url)
    if u.role == UserRole.student:
        st = await db.get(Student, u.id)
        if st:
            out.current_level_id = st.current_level_id
            out.placement_done = st.placement_done
    return out
